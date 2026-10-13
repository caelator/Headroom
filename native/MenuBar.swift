import Cocoa
import Foundation

private let checkPort:Int = {
    guard let index=CommandLine.arguments.firstIndex(of:"--check-port") else{return 8793}
    guard CommandLine.arguments.contains("--check"),index+1<CommandLine.arguments.count,
          let value=Int(CommandLine.arguments[index+1]),(1...65535).contains(value) else{
        print("Native check port must be a valid loopback port used with --check.");exit(64)
    }
    return value
}()
private let endpoint=URL(string:"http://127.0.0.1:\(checkPort)/api/plans")!
private let dashboard=URL(string:"http://127.0.0.1:8793/")!
private let dashboardEndpoint=URL(string:"http://127.0.0.1:8793/api/plans")!
private let maxResponseBytes=1_048_576
private let futureToleranceSeconds:TimeInterval=5
private let maximumFreshAgeSeconds:TimeInterval=90

private struct Plan {let verified_tier:String?}
private struct Pace {let pct_per_hour:Double?;let confidence:String;let exhausts_before_reset:Bool?;let eta_exhaustion_at:String?}
private struct Quota {
    let id:String;let label:String;let remaining_pct:Double?;let last_known_remaining_pct:Double?
    let resets_at:String?;let fresh_until:String?;let observed_at:String?;let status:String;let velocity:Pace
    var window_minutes:Double?=nil;var source:String?=nil
}
private struct Provider {
    let id:String;let name:String;let status:String;let remaining_pct:Double?;let plan:Plan;let windows:[Quota]
    let limiting_window_id:String?
}
private struct ServiceIdentity {let name:String;let api_version:Int;let source_fingerprint:String}
private struct FreshnessPolicy {let max_age_seconds:Double?}
private struct Snapshot {let updated_at:String;let providers:[Provider];let service:ServiceIdentity?;let freshness_policy:FreshnessPolicy?}

private let expectedFingerprint=Bundle.main.object(forInfoDictionaryKey:"HeadroomSourceFingerprint") as? String
private let isBundledApp=Bundle.main.bundleURL.pathExtension=="app"
private func compatible(_ snapshot:Snapshot,expected:String?=expectedFingerprint,bundled:Bool=isBundledApp)->Bool {
    guard snapshot.service?.name=="headroom", snapshot.service?.api_version==1 else{return false}
    guard let expected, !expected.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty else{return !bundled}
    return snapshot.service?.source_fingerprint==expected
}

// Cached formatters: ISO8601DateFormatter/DateFormatter construction is comparatively expensive.
private let isoFractionalFormatter=ISO8601DateFormatter()
private let isoPlainFormatter=ISO8601DateFormatter()
private let resetFormatter:DateFormatter = {
    let formatter=DateFormatter();formatter.dateStyle = .medium;formatter.timeStyle = .short;return formatter
}()
private func parsedDate(_ text:String?)->Date? {
    guard let text, !text.isEmpty else{return nil}
    isoFractionalFormatter.formatOptions=[.withInternetDateTime,.withFractionalSeconds]
    return isoFractionalFormatter.date(from:text) ?? isoPlainFormatter.date(from:text)
}

private func jsonNumber(_ value:Any?)->Double? {
    guard let number=value as? NSNumber else{return nil}
    if CFGetTypeID(number)==CFBooleanGetTypeID(){return nil}
    let result=number.doubleValue;return result.isFinite ? result : nil
}
private func jsonBool(_ value:Any?)->Bool? {
    guard let number=value as? NSNumber, CFGetTypeID(number)==CFBooleanGetTypeID() else{return nil}
    return number.boolValue
}
private func jsonString(_ value:Any?)->String? {
    if let text=value as? String {return text}
    if let number=jsonNumber(value) {
        return number.rounded()==number && abs(number)<9_007_199_254_740_992 ? String(Int(number)) : String(number)
    }
    return nil
}

// Loss-tolerant decoding: a malformed provider or window is dropped without
// discarding the remaining providers/windows from an otherwise valid snapshot.
private func parseSnapshot(_ data:Data)->Snapshot? {
    guard data.count<=maxResponseBytes,
          let object=try? JSONSerialization.jsonObject(with:data),
          let root=object as? [String:Any],
          let rawProviders=root["providers"] as? [Any] else{return nil}
    let providers:[Provider]=rawProviders.compactMap{ item in
        guard let provider=item as? [String:Any], let id=jsonString(provider["id"]), !id.isEmpty else{return nil}
        let rawWindows=(provider["windows"] as? [Any]) ?? []
        let windows:[Quota]=rawWindows.compactMap{ entry in
            guard let window=entry as? [String:Any] else{return nil}
            let windowID=jsonString(window["id"]) ?? ""
            let velocityRaw=window["velocity"] as? [String:Any]
            let pace=Pace(pct_per_hour:jsonNumber(velocityRaw?["pct_per_hour"]),
                          confidence:jsonString(velocityRaw?["confidence"]) ?? "insufficient",
                          exhausts_before_reset:jsonBool(velocityRaw?["exhausts_before_reset"]),
                          eta_exhaustion_at:jsonString(velocityRaw?["eta_exhaustion_at"]))
            return Quota(id:windowID,label:jsonString(window["label"]) ?? (windowID.isEmpty ? "Window" : windowID),
                         remaining_pct:jsonNumber(window["remaining_pct"]),
                         last_known_remaining_pct:jsonNumber(window["last_known_remaining_pct"]),
                         resets_at:jsonString(window["resets_at"]),fresh_until:jsonString(window["fresh_until"]),
                         observed_at:jsonString(window["observed_at"]),
                         status:jsonString(window["status"]) ?? "unknown",velocity:pace,
                         window_minutes:jsonNumber(window["window_minutes"]),source:jsonString(window["source"]))
        }
        let planRaw=provider["plan"] as? [String:Any]
        return Provider(id:id,name:jsonString(provider["name"]) ?? id,
                        status:jsonString(provider["status"]) ?? "unavailable",
                        remaining_pct:jsonNumber(provider["remaining_pct"]),
                        plan:Plan(verified_tier:jsonString(planRaw?["verified_tier"])),
                        windows:windows,limiting_window_id:jsonString(provider["limiting_window_id"]))
    }
    var service:ServiceIdentity? = nil
    if let raw=root["service"] as? [String:Any], let name=jsonString(raw["name"]),
       let version=jsonNumber(raw["api_version"]), version.rounded()==version,
       version>=Double(Int.min), version<=Double(Int.max),
       let fingerprint=jsonString(raw["source_fingerprint"]) {
        service=ServiceIdentity(name:name,api_version:Int(version),source_fingerprint:fingerprint)
    }
    var policy:FreshnessPolicy? = nil
    if let raw=root["freshness_policy"] as? [String:Any] {policy=FreshnessPolicy(max_age_seconds:jsonNumber(raw["max_age_seconds"]))}
    return Snapshot(updated_at:jsonString(root["updated_at"]) ?? "",providers:providers,service:service,freshness_policy:policy)
}

private func sessionConfiguration()->URLSessionConfiguration {
    let c=URLSessionConfiguration.ephemeral;c.connectionProxyDictionary=[:]
    c.timeoutIntervalForRequest=4;c.timeoutIntervalForResource=5
    c.waitsForConnectivity=false;c.urlCache=nil;c.httpCookieStorage=nil
    return c
}

// Streaming loader: it refuses redirects and cancels as soon as the body
// exceeds maxResponseBytes instead of buffering an unbounded response first.
// Every completion is delivered on the main queue.
private final class BoundedLoader:NSObject,URLSessionDataDelegate {
    private final class State {
        var data=Data();var response:URLResponse?;var oversized=false
        let completion:(Data?,URLResponse?,Error?)->Void
        init(_ completion:@escaping (Data?,URLResponse?,Error?)->Void){self.completion=completion}
    }
    private var states:[Int:State]=[:]
    private let lock=NSLock()
    private lazy var urlSession:URLSession = {
        let queue=OperationQueue();queue.maxConcurrentOperationCount=1
        return URLSession(configuration:sessionConfiguration(),delegate:self,delegateQueue:queue)
    }()
    func load(_ url:URL,completion:@escaping (Data?,URLResponse?,Error?)->Void)->URLSessionDataTask {
        load(URLRequest(url:url),completion:completion)
    }
    func load(_ request:URLRequest,completion:@escaping (Data?,URLResponse?,Error?)->Void)->URLSessionDataTask {
        let task=urlSession.dataTask(with:request)
        lock.lock();states[task.taskIdentifier]=State(completion);lock.unlock()
        task.resume();return task
    }
    private func state(for task:URLSessionTask)->State? {lock.lock();defer{lock.unlock()};return states[task.taskIdentifier]}
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping (URLRequest?)->Void) {
        completionHandler(nil) // Refuse redirects: the 3xx response itself is delivered and callers require 200.
    }
    func urlSession(_ session:URLSession,dataTask:URLSessionDataTask,didReceive response:URLResponse,completionHandler:@escaping (URLSession.ResponseDisposition)->Void) {
        guard let state=state(for:dataTask) else{completionHandler(.cancel);return}
        state.response=response
        if response.expectedContentLength>Int64(maxResponseBytes) {
            state.oversized=true;completionHandler(.cancel);return
        }
        completionHandler(.allow)
    }
    func urlSession(_ session:URLSession,dataTask:URLSessionDataTask,didReceive data:Data) {
        guard let state=state(for:dataTask) else{dataTask.cancel();return}
        if state.data.count+data.count>maxResponseBytes {state.oversized=true;state.data.removeAll();dataTask.cancel();return}
        state.data.append(data)
    }
    func urlSession(_ session:URLSession,task:URLSessionTask,didCompleteWithError error:Error?) {
        lock.lock();let state=states.removeValue(forKey:task.taskIdentifier);lock.unlock()
        guard let state else{return}
        let oversizedError=NSError(domain:"HeadroomNative",code:1,userInfo:[NSLocalizedDescriptionKey:"Local service response exceeded the size limit."])
        let deliveredError=state.oversized ? oversizedError : error
        let deliveredData=state.oversized ? nil : state.data
        let completion=state.completion
        DispatchQueue.main.async{completion(deliveredData,state.response,deliveredError)}
    }
}

private func validPercent(_ value:Double?)->Double? {
    guard let value,value.isFinite,value>=0,value<=100 else{return nil}
    return value
}
private func percent(_ value:Double?)->String {
    guard let value=validPercent(value) else{return "unknown"}
    return String(format:value<1 ? "%.2f%%" : "%.1f%%",value)
}

private enum WindowState:String {case fresh,stale,unknown,expired,unlimited}
private func freshnessDeadline(_ w:Quota,maxAgeSeconds:Double?=nil,at now:Date)->Date? {
    guard let observed=parsedDate(w.observed_at), observed<=now.addingTimeInterval(futureToleranceSeconds) else{return nil}
    let supplied=(maxAgeSeconds.map{ $0.isFinite && $0>0 ? $0 : nil } ?? nil)
    let age=min(maximumFreshAgeSeconds,supplied ?? maximumFreshAgeSeconds)
    let observedDeadline=observed.addingTimeInterval(age)
    guard let declared=parsedDate(w.fresh_until) else{return observedDeadline}
    return min(observedDeadline,declared)
}
private let degradedProviderStatuses:Set<String>=["auth_required","unavailable","error","stale"]
private func windowState(_ w:Quota,providerStatus:String,maxAgeSeconds:Double?=nil,at now:Date)->WindowState {
    var state=WindowState(rawValue:w.status) ?? .unknown
    if state == .fresh || state == .unlimited {
        if let reset=parsedDate(w.resets_at), reset<=now {state = .expired}
        else if freshnessDeadline(w,maxAgeSeconds:maxAgeSeconds,at:now).map({$0<=now}) ?? true {state = .stale}
        else if degradedProviderStatuses.contains(providerStatus) {state = .stale}
    }
    return state
}

private struct Selection {let window:Quota;let state:WindowState;let providerIndex:Int}
private func selection(_ p:Provider,providerIndex:Int=0,maxAgeSeconds:Double?=nil,at now:Date)->Selection? {
    let wins=p.windows.map{(window:$0,state:windowState($0,providerStatus:p.status,maxAgeSeconds:maxAgeSeconds,at:now))}
    guard !wins.isEmpty else{return nil}
    let measured=wins.filter{$0.state == .fresh && validPercent($0.window.remaining_pct) != nil}
    if let exhausted=measured.first(where:{$0.window.remaining_pct==0}) {return Selection(window:exhausted.window,state:exhausted.state,providerIndex:providerIndex)}
    let risks=measured.filter{ candidate in
        guard candidate.window.velocity.confidence=="observed", candidate.window.velocity.exhausts_before_reset==true,
              let eta=parsedDate(candidate.window.velocity.eta_exhaustion_at), eta>now,
              let reset=parsedDate(candidate.window.resets_at), eta<reset else{return false}
        return true
    }
    if let risk=risks.enumerated().min(by:{ lhs,rhs in
        let left=parsedDate(lhs.element.window.velocity.eta_exhaustion_at) ?? .distantFuture
        let right=parsedDate(rhs.element.window.velocity.eta_exhaustion_at) ?? .distantFuture
        return left==right ? lhs.offset<rhs.offset : left<right
    }) {return Selection(window:risk.element.window,state:risk.element.state,providerIndex:providerIndex)}
    if !measured.isEmpty {
        let chosen=measured.reduce(measured[0]){ (validPercent($0.window.remaining_pct) ?? 101) <= (validPercent($1.window.remaining_pct) ?? 101) ? $0 : $1 }
        return Selection(window:chosen.window,state:chosen.state,providerIndex:providerIndex)
    }
    if let unlimited=wins.first(where:{$0.state == .unlimited}) {return Selection(window:unlimited.window,state:.unlimited,providerIndex:providerIndex)}
    if let limiting=wins.first(where:{$0.window.id==p.limiting_window_id}) {return Selection(window:limiting.window,state:limiting.state,providerIndex:providerIndex)}
    let first=wins[0];return Selection(window:first.window,state:first.state,providerIndex:providerIndex)
}
private func currentRemaining(_ p:Provider,maxAgeSeconds:Double?=nil,at now:Date)->Double? {
    guard let selected=selection(p,maxAgeSeconds:maxAgeSeconds,at:now), selected.state == .fresh else{return nil}
    return validPercent(selected.window.remaining_pct)
}
private func isUnlimited(_ p:Provider,maxAgeSeconds:Double?=nil,at now:Date)->Bool {
    selection(p,maxAgeSeconds:maxAgeSeconds,at:now)?.state == .unlimited
}
private func lastKnownRemaining(_ p:Provider,maxAgeSeconds:Double?=nil,at now:Date)->Double? {
    guard let selected=selection(p,maxAgeSeconds:maxAgeSeconds,at:now), selected.state != .fresh, selected.state != .unlimited else{return nil}
    return validPercent(selected.window.last_known_remaining_pct) ?? validPercent(selected.window.remaining_pct)
}
private func measured(_ p:Provider,maxAgeSeconds:Double?=nil,at now:Date=Date())->Bool {currentRemaining(p,maxAgeSeconds:maxAgeSeconds,at:now) != nil}

// Pure geometry helper, exercised by --self-test.
private func clampedFrame(_ frame:NSRect,into visible:NSRect)->NSRect {
    var result=frame
    result.origin.x = frame.width>=visible.width ? visible.minX : min(max(frame.minX,visible.minX),visible.maxX-frame.width)
    result.origin.y = frame.height>=visible.height ? visible.minY : min(max(frame.minY,visible.minY),visible.maxY-frame.height)
    return result
}

private func widgetPercent(_ value:Double)->String {
    return String(format:value.rounded()==value ? "%.0f%%" : "%.1f%%",value)
}
private func resetCountdown(_ date:Date,at now:Date)->String {
    let minutes=max(1,Int(ceil(date.timeIntervalSince(now)/60)))
    if minutes>=1440 {return "\(minutes/1440)d \((minutes%1440)/60)h"}
    if minutes>=60 {return "\(minutes/60)h \(minutes%60)m"}
    return "\(minutes)m"
}
private struct WidgetReading {
    let value:String;let detail:String;let meter:Double?;let state:WindowState
}
private func widgetReading(_ window:Quota,providerStatus:String,serviceStale:Bool,maxAgeSeconds:Double?,at now:Date)->WidgetReading {
    var state=windowState(window,providerStatus:providerStatus,maxAgeSeconds:maxAgeSeconds,at:now)
    if serviceStale && (state == .fresh || state == .unlimited) {state = .stale}
    let reset=parsedDate(window.resets_at)
    let resetText=reset.map{$0>now ? "Resets in "+resetCountdown($0,at:now) : "Reset passed"} ?? "Reset not reported"
    if state == .fresh,let remaining=validPercent(window.remaining_pct) {
        return WidgetReading(value:widgetPercent(remaining),detail:resetText,meter:remaining,state:state)
    }
    if state == .unlimited {return WidgetReading(value:"Unlimited",detail:"No quota cap reported",meter:nil,state:state)}
    let known=validPercent(window.last_known_remaining_pct) ?? validPercent(window.remaining_pct)
    if state == .expired {
        return WidgetReading(value:"Reset passed",detail:known.map{"Last "+widgetPercent($0)+" before reset"} ?? "Awaiting a new reading",meter:nil,state:state)
    }
    if let known {return WidgetReading(value:"Last "+widgetPercent(known),detail:"Not current · awaiting update",meter:nil,state:state)}
    return WidgetReading(value:providerStatus=="auth_required" ? "Sign in" : "Unavailable",detail:state == .stale ? "Awaiting a fresh reading" : resetText,meter:nil,state:state)
}

private final class WidgetMeter:NSView {
    var remaining:Double=0 {didSet{needsDisplay=true}}
    override func draw(_ dirtyRect:NSRect) {
        let track=NSBezierPath(roundedRect:bounds,xRadius:2,yRadius:2)
        NSColor.separatorColor.withAlphaComponent(0.3).setFill();track.fill()
        let filled=NSRect(x:bounds.minX,y:bounds.minY,width:bounds.width*remaining/100,height:bounds.height)
        (remaining<=10 ? NSColor.systemRed : (remaining<=25 ? .systemOrange : .systemGreen)).setFill()
        NSBezierPath(roundedRect:filled,xRadius:2,yRadius:2).fill()
    }
}
private final class WidgetQuotaTile {
    let view=NSBox();let label=NSTextField(labelWithString:"")
    let value=NSTextField(labelWithString:"—");let detail=NSTextField(labelWithString:"")
    let progress=WidgetMeter()
    init(provider:String,window:Quota) {
        view.boxType = .custom;view.borderWidth=0;view.titlePosition = .noTitle
        view.contentViewMargins = .zero;view.cornerRadius=8
        view.fillColor = .controlBackgroundColor.withAlphaComponent(0.55)
        let content=NSView();view.contentView=content
        let stack=NSStackView();stack.orientation = .vertical;stack.alignment = .leading;stack.spacing=3
        stack.translatesAutoresizingMaskIntoConstraints=false;content.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo:content.leadingAnchor,constant:8),
          stack.trailingAnchor.constraint(equalTo:content.trailingAnchor,constant:-8),
          stack.topAnchor.constraint(equalTo:content.topAnchor,constant:6),
          stack.bottomAnchor.constraint(equalTo:content.bottomAnchor,constant:-6)])
        label.stringValue=window.label;label.font = .systemFont(ofSize:10,weight:.medium)
        label.textColor = .secondaryLabelColor;label.lineBreakMode = .byWordWrapping;label.maximumNumberOfLines=2
        label.setContentCompressionResistancePriority(.defaultLow,for:.horizontal)
        value.font = .monospacedDigitSystemFont(ofSize:18,weight:.semibold)
        value.setContentCompressionResistancePriority(.required,for:.horizontal)
        let top=NSStackView(views:[label,value]);top.orientation = .horizontal;top.alignment = .centerY
        top.distribution = .fill;top.spacing=6
        progress.heightAnchor.constraint(equalToConstant:4).isActive=true
        progress.setAccessibilityElement(true);progress.setAccessibilityRole(.progressIndicator)
        progress.setAccessibilityLabel(provider+" "+window.label+" remaining")
        detail.font = .systemFont(ofSize:10);detail.textColor = .secondaryLabelColor;detail.lineBreakMode = .byTruncatingTail
        stack.addArrangedSubview(top);stack.addArrangedSubview(progress);stack.addArrangedSubview(detail)
        for child in stack.arrangedSubviews {child.widthAnchor.constraint(equalTo:stack.widthAnchor).isActive=true}
        view.heightAnchor.constraint(greaterThanOrEqualToConstant:54).isActive=true
    }
    func update(_ reading:WidgetReading,provider:String,window:Quota) {
        value.stringValue=reading.value
        let current=reading.state == .fresh || reading.state == .unlimited
        value.font = .monospacedDigitSystemFont(ofSize:current ? 18 : 12,weight:.semibold)
        value.textColor=reading.meter.map{$0<=10 ? .systemRed : ($0<=25 ? .systemOrange : .labelColor)} ?? (current ? .labelColor : .secondaryLabelColor)
        detail.stringValue=reading.detail
        // Historical percentages are text only; a meter always means a current reading.
        progress.isHidden=reading.meter==nil;progress.remaining=reading.meter ?? 0
        progress.setAccessibilityValue(reading.meter.map{widgetPercent($0)+" remaining"} ?? "No current percentage")
        value.setAccessibilityLabel(provider+" "+window.label+": "+reading.value+(reading.meter != nil ? " remaining" : ""))
        let resetDetail=parsedDate(window.resets_at).map{"\nReset: "+resetFormatter.string(from:$0)} ?? ""
        let durationDetail=window.window_minutes.flatMap{ $0>0 && $0<=5_270_400 ? "\nReported window: \(Int($0)) minutes" : nil } ?? ""
        let sourceDetail=window.source.map{"\nSource: "+$0} ?? ""
        view.toolTip=[provider,window.label,reading.value,reading.detail].joined(separator:" · ")+resetDetail+durationDetail+sourceDetail
    }
}
private final class WidgetProviderGroup {
    let view=NSStackView();let status=NSTextField(labelWithString:"")
    var tiles:[WidgetQuotaTile]=[]
    init(_ provider:Provider) {
        view.orientation = .vertical;view.alignment = .leading;view.spacing=5
        let name=NSTextField(labelWithString:provider.id=="codex" ? "Codex / ChatGPT" : provider.name)
        name.font = .systemFont(ofSize:12,weight:.semibold)
        status.font = .systemFont(ofSize:9);status.textColor = .secondaryLabelColor;status.alignment = .right
        let header=NSStackView(views:[name,status]);header.orientation = .horizontal;header.distribution = .fill;header.spacing=8
        name.setContentHuggingPriority(.defaultLow,for:.horizontal)
        view.addArrangedSubview(header);header.widthAnchor.constraint(equalTo:view.widthAnchor).isActive=true
        if provider.windows.isEmpty {
            let empty=NSTextField(labelWithString:"No quota windows reported");empty.font = .systemFont(ofSize:11)
            empty.textColor = .secondaryLabelColor;view.addArrangedSubview(empty)
        }
        for start in stride(from:0,to:provider.windows.count,by:2) {
            let pair=NSStackView();pair.orientation = .horizontal;pair.distribution = .fillEqually;pair.spacing=6
            for index in start..<min(start+2,provider.windows.count) {
                let tile=WidgetQuotaTile(provider:provider.name,window:provider.windows[index]);tiles.append(tile)
                pair.addArrangedSubview(tile.view)
            }
            view.addArrangedSubview(pair);pair.widthAnchor.constraint(equalTo:view.widthAnchor).isActive=true
        }
    }
}

private final class FloatingWidget:NSObject,NSWindowDelegate {
    private let panel=NSPanel(contentRect:NSRect(x:40,y:80,width:420,height:540),
      styleMask:[.borderless,.nonactivatingPanel],backing:.buffered,defer:false)
    private let subtitle=NSTextField(labelWithString:"Quota remaining · provider-reported windows")
    private let footnote=NSTextField(labelWithString:"Connecting to your local service…")
    private let actionStatus=NSTextField(labelWithString:"")
    private let checkButton=NSButton(title:"Check now",target:nil,action:nil)
    private let rowsStack=NSStackView();private let scroll=NSScrollView()
    private var groups:[WidgetProviderGroup]=[];private var rowSignature:[String]=[]
    private var screenObserver:NSObjectProtocol?
    private let open:()->Void;private let check:()->Void
    private let defaults=UserDefaults.standard
    var isVisible:Bool {panel.isVisible};var panelVisible:Bool {panel.isVisible}
    init(openDashboard:@escaping ()->Void,checkNow:@escaping ()->Void={},testing:Bool=false) {
        self.open=openDashboard;self.check=checkNow;super.init()
        if !testing {defaults.register(defaults:["floatingWidgetEnabled":true])}
        if !testing,defaults.object(forKey:"NSWindow Frame HeadroomFloatingWidget")==nil,
           let frame=UserDefaults(suiteName:"com.xxx.codex-usage-menu")?.string(forKey:"NSWindow Frame HeadroomFloatingWidget") {
            defaults.set(frame,forKey:"NSWindow Frame HeadroomFloatingWidget")
        }
        panel.title="Headroom floating widget";panel.level = .floating
        panel.collectionBehavior=[.canJoinAllSpaces,.fullScreenAuxiliary]
        panel.isOpaque=false;panel.backgroundColor = .clear;panel.hasShadow=true
        panel.isFloatingPanel=true;panel.hidesOnDeactivate=false
        panel.becomesKeyOnlyIfNeeded=true;panel.isMovableByWindowBackground=true
        panel.isReleasedWhenClosed=false;panel.delegate=self
        let surface=NSVisualEffectView();surface.material = .hudWindow;surface.blendingMode = .behindWindow;surface.state = .active
        surface.wantsLayer=true;surface.layer?.cornerRadius=16;surface.layer?.masksToBounds=true;panel.contentView=surface
        let stack=NSStackView();stack.orientation = .vertical;stack.alignment = .leading;stack.spacing=10
        stack.translatesAutoresizingMaskIntoConstraints=false;surface.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo:surface.leadingAnchor,constant:14),
          stack.trailingAnchor.constraint(equalTo:surface.trailingAnchor,constant:-14),
          stack.topAnchor.constraint(equalTo:surface.topAnchor,constant:14),
          stack.bottomAnchor.constraint(equalTo:surface.bottomAnchor,constant:-12)])
        let title=NSTextField(labelWithString:"◉  Headroom");title.font = .systemFont(ofSize:14,weight:.semibold)
        let hide=NSButton(title:"Hide",target:self,action:#selector(hideWidget));hide.bezelStyle = .inline;hide.setAccessibilityLabel("Hide floating widget")
        let spacer=NSView();spacer.setContentHuggingPriority(.defaultLow,for:.horizontal)
        let header=NSStackView(views:[title,spacer,hide]);header.orientation = .horizontal
        subtitle.font = .systemFont(ofSize:11);subtitle.textColor = .secondaryLabelColor
        stack.addArrangedSubview(header);stack.addArrangedSubview(subtitle)
        scroll.hasVerticalScroller=true;scroll.autohidesScrollers=true;scroll.drawsBackground=false;scroll.borderType = .noBorder
        scroll.horizontalScrollElasticity = .none;scroll.verticalScrollElasticity = .automatic
        let document=FlippedWidgetDocument();document.translatesAutoresizingMaskIntoConstraints=false
        scroll.documentView=document
        rowsStack.orientation = .vertical;rowsStack.alignment = .leading;rowsStack.spacing=12
        rowsStack.translatesAutoresizingMaskIntoConstraints=false;document.addSubview(rowsStack)
        NSLayoutConstraint.activate([document.widthAnchor.constraint(equalTo:scroll.contentView.widthAnchor),
          rowsStack.leadingAnchor.constraint(equalTo:document.leadingAnchor),rowsStack.trailingAnchor.constraint(equalTo:document.trailingAnchor),
          rowsStack.topAnchor.constraint(equalTo:document.topAnchor),rowsStack.bottomAnchor.constraint(equalTo:document.bottomAnchor)])
        stack.addArrangedSubview(scroll)
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant:80).isActive=true
        footnote.font = .systemFont(ofSize:10);footnote.textColor = .secondaryLabelColor
        actionStatus.font = .systemFont(ofSize:10);actionStatus.textColor = .secondaryLabelColor
        stack.addArrangedSubview(footnote);stack.addArrangedSubview(actionStatus);actionStatus.isHidden=true
        checkButton.target=self;checkButton.action=#selector(FloatingWidget.checkNow);checkButton.bezelStyle = .rounded
        checkButton.setAccessibilityLabel("Check provider quotas now")
        let openButton=NSButton(title:"Open dashboard ↗",target:self,action:#selector(FloatingWidget.openDashboard));openButton.bezelStyle = .rounded
        openButton.setAccessibilityLabel("Open Headroom dashboard")
        let actions=NSStackView(views:[checkButton,openButton]);actions.orientation = .horizontal;actions.distribution = .fillEqually;actions.spacing=8
        stack.addArrangedSubview(actions)
        for view in stack.arrangedSubviews {view.widthAnchor.constraint(equalTo:stack.widthAnchor).isActive=true}
        if !testing {
            panel.setFrameAutosaveName("HeadroomFloatingWidget")
            if !panel.setFrameUsingName("HeadroomFloatingWidget") {placeOnScreen()}
        }
        // Preserve the saved position, while adopting this version's wider layout.
        var frame=panel.frame;frame.size.width=420;panel.setFrame(frame,display:true);clampPanelFrame()
        screenObserver=NotificationCenter.default.addObserver(forName:NSApplication.didChangeScreenParametersNotification,object:nil,queue:.main){[weak self] _ in self?.fitPanel()}
        if !testing && defaults.bool(forKey:"floatingWidgetEnabled") {panel.orderFrontRegardless()}
    }
    deinit {if let screenObserver {NotificationCenter.default.removeObserver(screenObserver)}}
    private func placeOnScreen() {
        guard let frame=NSScreen.main?.visibleFrame else{return}
        panel.setFrameOrigin(NSPoint(x:frame.maxX-panel.frame.width-24,y:frame.minY+24))
    }
    private func targetScreen()->NSScreen? {
        NSScreen.screens.max(by:{$0.visibleFrame.intersection(panel.frame).area < $1.visibleFrame.intersection(panel.frame).area}) ?? NSScreen.main
    }
    func clampPanelFrame() {if let screen=targetScreen(){panel.setFrame(clampedFrame(panel.frame,into:screen.visibleFrame),display:true)}}
    private func fitPanel() {
        panel.contentView?.layoutSubtreeIfNeeded()
        let contentHeight=rowsStack.fittingSize.height
        let available=targetScreen()?.visibleFrame.height ?? 900
        let desired=min(max(260,contentHeight+148+(actionStatus.isHidden ? 0 : 24)),min(700,max(220,available-48)))
        if abs(panel.frame.height-desired)>1 {
            var frame=panel.frame;let top=frame.maxY;frame.size.height=desired;frame.origin.y=top-desired
            panel.setFrame(frame,display:true)
        }
        clampPanelFrame();panel.contentView?.layoutSubtreeIfNeeded()
    }
    private func rebuildRows(_ providers:[Provider]) {
        let signature=providers.map{$0.id+"\u{1f}"+$0.name+"\u{1f}"+$0.windows.map{$0.id+"\u{1e}"+$0.label}.joined(separator:"\u{1d}")}
        guard signature != rowSignature else{return}
        rowSignature=signature;groups.removeAll()
        for view in rowsStack.arrangedSubviews {rowsStack.removeArrangedSubview(view);view.removeFromSuperview()}
        for provider in providers {
            let group=WidgetProviderGroup(provider);groups.append(group);rowsStack.addArrangedSubview(group.view)
            group.view.widthAnchor.constraint(equalTo:rowsStack.widthAnchor).isActive=true
        }
        fitPanel()
    }
    func setVisible(_ visible:Bool) {
        defaults.set(visible,forKey:"floatingWidgetEnabled")
        if visible {fitPanel();panel.orderFrontRegardless()} else {panel.orderOut(nil)}
    }
    func setChecking(_ checking:Bool,message:String?=nil) {
        checkButton.isEnabled = !checking;checkButton.title=checking ? "Requesting…" : "Check now"
        actionStatus.stringValue=message ?? "";actionStatus.isHidden=message==nil;fitPanel()
    }
    func update(_ providers:[Provider],stale:Bool,maxAgeSeconds:Double?=nil,at now:Date=Date()) {
        rebuildRows(providers);var currentProviders=0
        for (index,provider) in providers.enumerated() {
            let group=groups[index];var currentWindows=0
            for (windowIndex,window) in provider.windows.enumerated() {
                let reading=widgetReading(window,providerStatus:provider.status,serviceStale:stale,maxAgeSeconds:maxAgeSeconds,at:now)
                group.tiles[windowIndex].update(reading,provider:provider.name,window:window)
                if reading.meter != nil || reading.state == .unlimited {currentWindows+=1}
            }
            if currentWindows>0 {currentProviders+=1}
            group.status.stringValue=provider.status=="auth_required" ? "Sign in required" :
              (provider.status=="error" ? "Connection error" : (currentWindows>0 ? "\(currentWindows)/\(provider.windows.count) current" : "Awaiting update"))
        }
        footnote.stringValue=providers.isEmpty ? "Connecting to your local service…" : "\(currentProviders)/\(providers.count) plans current · separate allowances"
        fitPanel()
    }
    func windowShouldClose(_ sender:NSWindow)->Bool {setVisible(false);return false}
    func assertLayoutRows(_ expected:Int,windows:Int?=nil) {
        panel.contentView?.layoutSubtreeIfNeeded()
        precondition(rowsStack.arrangedSubviews.count==expected && groups.count==expected)
        if let windows {precondition(groups.reduce(0){$0+$1.tiles.count}==windows)}
        precondition(scroll.contentView.bounds.height>0)
    }
    func assertValues(_ expected:[[String]]) {
        precondition(groups.map{$0.tiles.map{$0.value.stringValue}}==expected)
    }
    @objc private func hideWidget(){setVisible(false)}
    @objc private func openDashboard(){open()}
    @objc private func checkNow(){check()}
}
private final class FlippedWidgetDocument:NSView {override var isFlipped:Bool {true}}

private extension NSRect {var area:CGFloat {isNull || isEmpty ? 0 : width*height}}

private final class Menu:NSObject,NSApplicationDelegate,NSMenuDelegate {
    private var status:NSStatusItem!;private let menu=NSMenu()
    private let transport=BoundedLoader();private var snapshot:Snapshot?
    private var fetching=false;private var failed=false;private var fetchedAt:Date?
    private var timer:Timer?;private var expiryTimer:Timer?;private var task:URLSessionDataTask?
    private var dashboardTask:URLSessionDataTask?;private var openingDashboard=false
    private var refreshTask:URLSessionDataTask?;private var checkingQuotas=false
    private var refreshFeedbackTimer:Timer?
    private var widget:FloatingWidget?
    private var lastEvidenceSignature:String?;private var lastEvidenceWrite:Date?
    func applicationDidFinishLaunching(_ n:Notification) {
        status=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        status.button?.image=NSImage(systemSymbolName:"gauge.with.dots.needle.67percent",accessibilityDescription:"AI plans")
        status.button?.imagePosition = .imageLeading;status.button?.title="Plans …"
        menu.autoenablesItems=false;menu.delegate=self;status.menu=menu
        widget=FloatingWidget(openDashboard:{[weak self] in self?.openDashboard()},checkNow:{[weak self] in self?.refresh()})
        if Bundle.main.object(forInfoDictionaryKey:"HeadroomShowWidgetAfterInstall") as? Bool == true,
           let installID=Bundle.main.object(forInfoDictionaryKey:"HeadroomInstallID") as? String,
           UserDefaults.standard.string(forKey:"revealedInstallID") != installID {
            widget?.setVisible(true);UserDefaults.standard.set(installID,forKey:"revealedInstallID")
        }
        poll();timer=Timer.scheduledTimer(withTimeInterval:10,repeats:true){[weak self] _ in self?.poll()}
        expiryTimer=Timer.scheduledTimer(withTimeInterval:1,repeats:true){[weak self] _ in self?.update()}
    }
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool {
        widget?.setVisible(true);return false
    }
    private var stale:Bool {failed || (fetchedAt.map{Date().timeIntervalSince($0)>30} ?? true)}
    private var maxAgeSeconds:Double? {snapshot?.freshness_policy?.max_age_seconds}
    private func poll() {
        guard !fetching else{return};fetching=true
        task=transport.load(endpoint){[weak self] data,response,error in
            // BoundedLoader already delivers on the main queue.
            guard let self else{return};self.fetching=false;self.task=nil
            let value=data.flatMap{parseSnapshot($0)}
            if error==nil,(response as? HTTPURLResponse)?.statusCode==200,let value,compatible(value) {
                self.snapshot=value;self.fetchedAt=Date();self.failed=false
            }else{self.failed=true}
            self.update()
        }
    }
    private func update() {
        guard let button=status.button else{return}
        let now=Date();let providers=snapshot?.providers ?? []
        let selections=providers.enumerated().map{selection($0.element,providerIndex:$0.offset,maxAgeSeconds:maxAgeSeconds,at:now)}
        let finite=providers.enumerated().filter{!stale && selections[$0.offset]?.state == .fresh}
            .sorted{(validPercent(selections[$0.offset]?.window.remaining_pct) ?? 101) < (validPercent(selections[$1.offset]?.window.remaining_pct) ?? 101)}
        let unlimitedCount=stale ? 0 : selections.filter{$0?.state == .unlimited}.count
        if let entry=finite.first,let remaining=validPercent(selections[entry.offset]?.window.remaining_pct) {
            let p=entry.element;let short=p.id=="codex" ? "Codex" : p.id=="antigravity" ? "AG" : p.name
            button.title=short+" "+percent(remaining)
            button.toolTip="Headroom · \(p.name): \(percent(remaining)) remaining in its current limiting quota window. \(finite.count+unlimitedCount) of \(providers.count) plans have current quota data."
        }else if unlimitedCount>0, let entry=providers.enumerated().first(where:{selections[$0.offset]?.state == .unlimited}) {
            button.title=entry.element.name+" Unlimited"
            button.toolTip="Headroom · \(entry.element.name): no quota cap reported. \(unlimitedCount) of \(providers.count) plans have current quota data."
        }else{button.title="Plans —";button.toolTip="Headroom · quota data unavailable; click for connection details."}
        button.setAccessibilityLabel(button.toolTip ?? "Headroom plans")
        widget?.update(providers,stale:stale,maxAgeSeconds:maxAgeSeconds,at:now)
        writeEvidence(buttonTitle:button.title,providers:providers.count,measured:stale ? 0 : finite.count,current:stale ? 0 : finite.count+unlimitedCount,now:now)
    }
    private func writeEvidence(buttonTitle:String,providers:Int,measured:Int,current:Int,now:Date) {
        // panel_visible records that the widget panel is ordered in; it does not prove it is unobscured.
        let panelVisible=widget?.panelVisible ?? false
        let signature=[buttonTitle,String(stale),String(providers),String(measured),String(current),String(panelVisible),expectedFingerprint ?? ""].joined(separator:"\u{1f}")
        let periodicDue=lastEvidenceWrite.map{now.timeIntervalSince($0)>=10} ?? true
        guard signature != lastEvidenceSignature || periodicDue else{return}
        lastEvidenceSignature=signature;lastEvidenceWrite=now
        let root=FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/aiusage/native-status.json")
        var evidence:[String:Any]=["updated_at":now.timeIntervalSince1970,"pid":Int(ProcessInfo.processInfo.processIdentifier),"title":buttonTitle,
          "stale":stale,"providers":providers,"measured_providers":measured,"current_providers":current,"panel_visible":panelVisible]
        evidence["source_fingerprint"]=expectedFingerprint
        if let bytes=try? JSONSerialization.data(withJSONObject:evidence){try? bytes.write(to:root,options:.atomic)}
    }
    private func line(_ text:String,into target:NSMenu?=nil) {
        let row=NSMenuItem(title:text,action:nil,keyEquivalent:"");row.isEnabled=false;(target ?? menu).addItem(row)
    }
    func menuWillOpen(_ m:NSMenu) {
        update();menu.removeAllItems();line("Headroom · plan allowances")
        if stale{line("Last-known data · refreshing connection")}
        let now=Date()
        for (index,p) in (snapshot?.providers ?? []).enumerated() {
            let selected=selection(p,providerIndex:index,maxAgeSeconds:maxAgeSeconds,at:now)
            let providerCurrent = !stale && selected?.state == .fresh
            let providerUnlimited = !stale && selected?.state == .unlimited
            let title:String
            if providerCurrent {title=p.name+" · "+percent(validPercent(selected?.window.remaining_pct))+" remaining"}
            else if providerUnlimited {title=p.name+" · Unlimited"}
            else if !stale, let known=lastKnownRemaining(p,maxAgeSeconds:maxAgeSeconds,at:now) {title=p.name+" · last known "+percent(known)+" · not current"}
            else {title=p.name+" · quota not current"}
            let row=NSMenuItem(title:title,action:nil,keyEquivalent:"");row.isEnabled=true;let sub=NSMenu()
            line(p.plan.verified_tier ?? "Plan tier not verified",into:sub)
            for w in p.windows {
                let state=windowState(w,providerStatus:p.status,maxAgeSeconds:maxAgeSeconds,at:now)
                let currentWindow = !stale && state == .fresh
                if currentWindow {line(w.label+": "+percent(w.remaining_pct)+" remaining",into:sub)}
                else if (!stale) && state == .unlimited {line(w.label+": unlimited · no quota cap reported",into:sub)}
                else {
                    let known=validPercent(w.last_known_remaining_pct) ?? validPercent(w.remaining_pct)
                    line(w.label+": not current ("+state.rawValue+")"+(known.map{" · last known "+percent($0)} ?? ""),into:sub)
                }
                // Forecasts and burn rates are shown only for a currently fresh window.
                if currentWindow,w.velocity.confidence=="observed",let rate=w.velocity.pct_per_hour,rate.isFinite {
                    line(String(format:"Observed net burn: %.2f percentage points/hour",rate),into:sub)
                }
                if let reset=w.resets_at,let date=parsedDate(reset){line("Reset: "+resetFormatter.string(from:date),into:sub)}
                if currentWindow,w.velocity.confidence=="observed",w.velocity.exhausts_before_reset==true,
                   let eta=parsedDate(w.velocity.eta_exhaustion_at),eta>now,let reset=parsedDate(w.resets_at),eta<reset {
                    line("⚠ Projected to exhaust before reset",into:sub)
                }
            }
            if p.windows.isEmpty{line("Quota feed not connected",into:sub)}
            row.submenu=sub;menu.addItem(row)
        }
        menu.addItem(.separator());line("Each plan has independent limits; percentages are not combined.")
        let widgetLabel=widget?.isVisible == true ? "Hide floating widget" : "Show floating widget"
        for (label,action) in [(widgetLabel,#selector(toggleWidget)),("Open plan dashboard",#selector(openDashboard)),("Check quotas now",#selector(refresh)),("Quit menu indicator",#selector(quit))] {
            let row=NSMenuItem(title:label,action:action,keyEquivalent:"");row.target=self;row.isEnabled=true;menu.addItem(row)
        }
    }
    @objc private func openDashboard(){
        guard !openingDashboard else{return}
        openingDashboard=true
        dashboardTask=transport.load(dashboardEndpoint){[weak self] data,response,error in
            // BoundedLoader already delivers on the main queue.
            guard let self else{return}
            self.openingDashboard=false;self.dashboardTask=nil
            let value=data.flatMap{parseSnapshot($0)}
            let qualified=error==nil && (response as? HTTPURLResponse)?.statusCode==200 && value.map{compatible($0)}==true
            if qualified && NSWorkspace.shared.open(dashboard) {return}
            self.showDashboardFailure()
        }
    }
    private func showDashboardFailure() {
        NSApplication.shared.activate(ignoringOtherApps:true)
        let alert=NSAlert();alert.alertStyle = .warning
        alert.messageText="Could not open the Headroom dashboard"
        alert.informativeText="The local Headroom service did not return a qualified Headroom snapshot, or the dashboard could not be opened. Start or reinstall Headroom, then choose Refresh in this menu. For a sanitized local check, run scripts/diagnostics.py from the Headroom folder."
        alert.addButton(withTitle:"OK");alert.runModal()
    }
    @objc private func toggleWidget(){widget?.setVisible(widget?.isVisible != true)}
    @objc private func refresh(){
        guard !checkingQuotas else{return}
        checkingQuotas=true;refreshFeedbackTimer?.invalidate();widget?.setChecking(true,message:"Verifying the local connection…")
        // Qualify service identity before sending a write to the loopback port.
        refreshTask=transport.load(dashboardEndpoint){[weak self] data,response,error in
            guard let self else{return}
            let qualified=error==nil && (response as? HTTPURLResponse)?.statusCode==200 && data.flatMap{parseSnapshot($0)}.map{compatible($0)}==true
            guard qualified else{self.finishRefresh(false);return}
            var request=URLRequest(url:URL(string:"http://127.0.0.1:8793/api/refresh")!)
            request.httpMethod="POST";request.httpBody=Data("{}".utf8)
            request.setValue("application/json",forHTTPHeaderField:"Content-Type")
            request.setValue("local",forHTTPHeaderField:"X-AIUsage-Action")
            request.setValue("http://127.0.0.1:8793",forHTTPHeaderField:"Origin")
            self.refreshTask=self.transport.load(request){[weak self] data,response,error in
                guard let self else{return}
                let payload=data.flatMap{try? JSONSerialization.jsonObject(with:$0)} as? [String:Any]
                self.finishRefresh(error==nil && (response as? HTTPURLResponse)?.statusCode==202 && jsonBool(payload?["synchronizing"])==true)
            }
        }
    }
    private func finishRefresh(_ success:Bool) {
        checkingQuotas=false;refreshTask=nil
        widget?.setChecking(false,message:success ? "Check queued · provider retry delays still apply" : "Check failed · open dashboard for connection details")
        if success {poll()}
        refreshFeedbackTimer=Timer.scheduledTimer(withTimeInterval:8,repeats:false){[weak self] _ in self?.widget?.setChecking(false)}
    }
    @objc private func quit(){timer?.invalidate();expiryTimer?.invalidate();refreshFeedbackTimer?.invalidate();task?.cancel();dashboardTask?.cancel();refreshTask?.cancel();NSApplication.shared.terminate(nil)}
}


private func syntheticWidgetProviders(at now:Date)->[Provider] {
    let formatter=ISO8601DateFormatter()
    func quota(_ id:String,_ label:String,_ remaining:Double?,_ hours:Double?,_ state:String="fresh",_ history:Double?=nil)->Quota {
        Quota(id:id,label:label,remaining_pct:remaining,last_known_remaining_pct:history,
            resets_at:hours.map{formatter.string(from:now.addingTimeInterval($0*3600))},
            fresh_until:formatter.string(from:now.addingTimeInterval(90)),observed_at:formatter.string(from:now),status:state,
            velocity:Pace(pct_per_hour:nil,confidence:"insufficient",exhausts_before_reset:nil,eta_exhaustion_at:nil))
    }
    func provider(_ id:String,_ name:String,_ windows:[Quota],_ status:String="verified")->Provider {
        Provider(id:id,name:name,status:status,remaining_pct:nil,plan:Plan(verified_tier:nil),windows:windows,limiting_window_id:nil)
    }
    return [provider("codex","Codex / ChatGPT",[quota("short","5-hour",93,3),quota("weekly","Weekly",62,144)]),
      provider("muse","Muse",[quota("window-300m","5-hour",88,2),quota("weekly-10080m","Weekly",37,72)]),
      provider("zai","z.ai",[quota("short","5-hour",100,nil),quota("weekly","Weekly",84,48),quota("mcp","MCP",96,600)]),
      provider("cursor","Cursor",[quota("included","Included plan",73,216),quota("models","Cursor models",99.9,216),quota("third","Third-party models",100,216)]),
      provider("antigravity","Antigravity",[quota("g-short","Gemini 5-hour",70,4),quota("g-weekly","Gemini weekly",40,120),quota("c-short","Claude/GPT 5-hour",55,4),quota("c-weekly","Claude/GPT weekly",90,144)]),
      provider("claude","Claude",[quota("short","5-hour",nil,2,"stale",42),quota("weekly","Weekly",nil,96,"stale",19)],"error")]
}

if CommandLine.arguments.contains("--widget-self-test") {
    let app=NSApplication.shared;app.setActivationPolicy(.accessory)
    let widget=FloatingWidget(openDashboard:{},testing:true)
    let now=Date();let formatter=ISO8601DateFormatter();let stamp=formatter.string(from:now)
    for count in [0,6,7,6,1,0] {
        let providers=(0..<count).map{ index in
            Provider(id:"fixture-\(index)",name:"Example provider \(index)",status:"verified",remaining_pct:nil,
                plan:Plan(verified_tier:nil),windows:[Quota(id:"window",label:"Example allowance",remaining_pct:Double(index*10),
                    last_known_remaining_pct:nil,resets_at:formatter.string(from:now.addingTimeInterval(3600)),
                    fresh_until:formatter.string(from:now.addingTimeInterval(90)),observed_at:stamp,status:"fresh",
                    velocity:Pace(pct_per_hour:nil,confidence:"insufficient",exhausts_before_reset:nil,eta_exhaustion_at:nil))],limiting_window_id:nil)
        }
        widget.update(providers,stale:false,at:now);widget.assertLayoutRows(count)
    }
    let fixtures=syntheticWidgetProviders(at:now)
    widget.update(fixtures,stale:false,at:now);widget.assertLayoutRows(6,windows:16)
    widget.assertValues([["93%","62%"],["88%","37%"],["100%","84%","96%"],["73%","99.9%","100%"],["70%","40%","55%","90%"],["Last 42%","Last 19%"]])
    let muse=fixtures[1];let weekly=muse.windows[1]
    let expired=widgetReading(weekly,providerStatus:"verified",serviceStale:false,maxAgeSeconds:nil,at:now.addingTimeInterval(73*3600))
    precondition(expired.value=="Reset passed" && expired.meter==nil && expired.detail=="Last 37% before reset")
    let serviceStale=widgetReading(muse.windows[0],providerStatus:"verified",serviceStale:true,maxAgeSeconds:nil,at:now)
    precondition(serviceStale.value=="Last 88%" && serviceStale.meter==nil)
    let exhausted=Quota(id:"empty",label:"Opus weekly",remaining_pct:0,last_known_remaining_pct:nil,resets_at:nil,
        fresh_until:formatter.string(from:now.addingTimeInterval(90)),observed_at:stamp,status:"fresh",
        velocity:Pace(pct_per_hour:nil,confidence:"insufficient",exhausts_before_reset:nil,eta_exhaustion_at:nil))
    precondition(widgetReading(exhausted,providerStatus:"verified",serviceStale:false,maxAgeSeconds:nil,at:now).meter==0)
    let unlimited=Quota(id:"unlimited",label:"Individual allowance",remaining_pct:nil,last_known_remaining_pct:nil,resets_at:nil,
        fresh_until:formatter.string(from:now.addingTimeInterval(90)),observed_at:stamp,status:"unlimited",velocity:exhausted.velocity)
    let unlimitedReading=widgetReading(unlimited,providerStatus:"verified",serviceStale:false,maxAgeSeconds:nil,at:now)
    precondition(unlimitedReading.value=="Unlimited" && unlimitedReading.meter==nil)
    widget.setChecking(true,message:"Checking");widget.assertLayoutRows(6,windows:16);widget.setChecking(false)
    print("Headroom native widget layout, every-window values, freshness, and dynamic row checks passed.");exit(0)
}

if CommandLine.arguments.contains("--widget-preview") || Bundle.main.object(forInfoDictionaryKey:"HeadroomSyntheticWidgetPreview") as? Bool == true {
    let app=NSApplication.shared;app.setActivationPolicy(.regular)
    let widget=FloatingWidget(openDashboard:{},testing:true)
    widget.update(syntheticWidgetProviders(at:Date()),stale:false);widget.setVisible(true)
    app.activate(ignoringOtherApps:true);withExtendedLifetime(widget){app.run()};exit(0)
}

if let fixtureIndex=CommandLine.arguments.firstIndex(of:"--selection-fixture") {
    guard fixtureIndex+1<CommandLine.arguments.count else{
        print("Usage: Headroom --selection-fixture PATH");exit(64)
    }
    let fixturePath=CommandLine.arguments[fixtureIndex+1]
    func fixtureRun()->Bool {
        guard let data=try? Data(contentsOf:URL(fileURLWithPath:fixturePath)), data.count<=maxResponseBytes,
              let object=try? JSONSerialization.jsonObject(with:data), let root=object as? [String:Any],
              jsonNumber(root["schema"])==1, let cases=root["cases"] as? [Any], !cases.isEmpty else{
            print("Selection fixture: unreadable, oversized, or unsupported fixture.");return false
        }
        let runnerRaw=root["runner"] as? [String:Any]
        let defaultsRaw=runnerRaw?["window_defaults"] as? [String:Any]
        let fixedNow=Date(timeIntervalSince1970:jsonNumber(runnerRaw?["fixed_now_epoch"]) ?? 1_800_000_000)
        let stamp=ISO8601DateFormatter()
        func iso(_ date:Date)->String {stamp.string(from:date)}
        var failures:[String]=[]
        for rawCase in cases {
            guard let item=rawCase as? [String:Any] else{failures.append("case is not an object");continue}
            let name=jsonString(item["name"]) ?? "unnamed case"
            func fail(_ message:String){failures.append(name+": "+message)}
            var rawWindows:[Any]=[]
            if let list=item["windows"] as? [Any] {rawWindows=list}
            else if let single=item["window"] as? [String:Any] {rawWindows=[single]}
            let windows:[Quota]=rawWindows.enumerated().map{ index, entry in
                let window=(entry as? [String:Any]) ?? [:]
                let velocityRaw=window["velocity"] as? [String:Any]
                let observed:String?
                if let explicit=jsonString(window["observed_at"]) {observed=explicit}
                else if let offset=jsonNumber(window["observed_offset_seconds"]) {observed=iso(fixedNow.addingTimeInterval(offset))}
                else {
                    let age=jsonNumber(window["observed_age_seconds"]) ?? jsonNumber(defaultsRaw?["observed_age_seconds"]) ?? 0
                    observed=iso(fixedNow.addingTimeInterval(-age))
                }
                let freshSeconds=jsonNumber(window["fresh_until_in_seconds"]) ?? jsonNumber(defaultsRaw?["fresh_until_in_seconds"]) ?? 3600
                let resetSeconds=jsonNumber(window["resets_in_seconds"]) ?? jsonNumber(defaultsRaw?["resets_in_seconds"]) ?? 3600
                let eta:String?
                if let etaSeconds=jsonNumber(window["eta_in_seconds"]) ?? jsonNumber(velocityRaw?["eta_in_seconds"]) {eta=iso(fixedNow.addingTimeInterval(etaSeconds))}
                else {eta=jsonString(window["eta_exhaustion_at"]) ?? jsonString(velocityRaw?["eta_exhaustion_at"])}
                let hasEta=eta != nil
                return Quota(id:jsonString(window["id"]) ?? "w\(index)",label:jsonString(window["label"]) ?? jsonString(window["id"]) ?? "Window",
                             remaining_pct:jsonNumber(window["remaining_pct"]),last_known_remaining_pct:jsonNumber(window["last_known_remaining_pct"]),
                             resets_at:iso(fixedNow.addingTimeInterval(resetSeconds)),fresh_until:iso(fixedNow.addingTimeInterval(freshSeconds)),
                             observed_at:observed,status:jsonString(window["status"]) ?? jsonString(defaultsRaw?["status"]) ?? "fresh",
                             velocity:Pace(pct_per_hour:jsonNumber(window["pct_per_hour"]) ?? jsonNumber(velocityRaw?["pct_per_hour"]),
                                           confidence:jsonString(window["confidence"]) ?? jsonString(velocityRaw?["confidence"]) ?? (hasEta ? "observed" : "insufficient"),
                                           exhausts_before_reset:jsonBool(window["exhausts_before_reset"]) ?? jsonBool(velocityRaw?["exhausts_before_reset"]) ?? (hasEta ? true : nil),
                                           eta_exhaustion_at:eta))
            }
            let provider=Provider(id:"fixture",name:"Fixture",status:jsonString(item["provider_status"]) ?? "verified",
                                  remaining_pct:nil,plan:Plan(verified_tier:nil),windows:windows,
                                  limiting_window_id:jsonString(item["limiting_window_id"]))
            let evaluated=fixedNow.addingTimeInterval(jsonNumber(item["evaluated_after_seconds"]) ?? 0)
            let maxAge=jsonNumber(item["max_age_seconds"])
            let selected=selection(provider,maxAgeSeconds:maxAge,at:evaluated)
            if let expected=jsonString(item["expected_window_id"]), selected?.window.id != expected {
                fail("expected window \(expected), got \(selected?.window.id ?? "none")")
            }
            if let expected=jsonString(item["expected_state"]), selected?.state.rawValue != expected {
                fail("expected state \(expected), got \(selected?.state.rawValue ?? "none")")
            }
            if item.keys.contains("expected_remaining_pct") {
                let expected=jsonNumber(item["expected_remaining_pct"]);let actual=currentRemaining(provider,maxAgeSeconds:maxAge,at:evaluated)
                if expected != actual {fail("expected remaining \(expected.map{String($0)} ?? "nil"), got \(actual.map{String($0)} ?? "nil")")}
            }
            if item.keys.contains("expected_last_known_pct") {
                let expected=jsonNumber(item["expected_last_known_pct"]);let actual=lastKnownRemaining(provider,maxAgeSeconds:maxAge,at:evaluated)
                if expected != actual {fail("expected last known \(expected.map{String($0)} ?? "nil"), got \(actual.map{String($0)} ?? "nil")")}
            }
            if jsonBool(item["historical_meter"])==false || jsonBool(item["historical_forecast"])==false {
                if currentRemaining(provider,maxAgeSeconds:maxAge,at:evaluated) != nil {fail("historical case unexpectedly has a current remaining value")}
            }
        }
        if failures.isEmpty {print("Headroom native selection fixture: \(cases.count) cases passed.");return true}
        for failure in failures {print("Selection fixture failure: "+failure)}
        return false
    }
    exit(fixtureRun() ? 0 : 1)
}

if CommandLine.arguments.contains("--self-test") {
    let now=Date(timeIntervalSince1970:1_800_000_000);let stamp=ISO8601DateFormatter()
    func quota(_ id:String,_ value:Double?,status:String="fresh",observed:Date?=nil,freshUntil:Date?=nil,reset:Date?=nil,
               lastKnown:Double?=nil,confidence:String="insufficient",exhausts:Bool?=nil,eta:Date?=nil)->Quota {
        Quota(id:id,label:id,remaining_pct:value,last_known_remaining_pct:lastKnown,
          resets_at:reset.map{stamp.string(from:$0)},fresh_until:freshUntil.map{stamp.string(from:$0)},
          observed_at:observed.map{stamp.string(from:$0)},status:status,
          velocity:Pace(pct_per_hour:nil,confidence:confidence,exhausts_before_reset:exhausts,eta_exhaustion_at:eta.map{stamp.string(from:$0)}))
    }
    func freshWindow(_ id:String,_ value:Double?,age:TimeInterval=0,declared:TimeInterval=3600,reset:TimeInterval=3600)->Quota {
        quota(id,value,observed:now.addingTimeInterval(-age),freshUntil:now.addingTimeInterval(declared),reset:now.addingTimeInterval(reset))
    }
    let plan=Plan(verified_tier:nil)
    func provider(_ status:String,_ windows:[Quota],_ limiting:String?=nil,id:String="test")->Provider {
        Provider(id:id,name:"Test",status:status,remaining_pct:0,plan:plan,windows:windows,limiting_window_id:limiting)
    }
    // Freshness: valid observed_at, <=90s cap, minimum declared deadline, no future/malformed evidence.
    precondition(windowState(freshWindow("a",75,age:89),providerStatus:"verified",at:now) == .fresh)
    precondition(windowState(freshWindow("a",75,age:91),providerStatus:"verified",at:now) == .stale)
    precondition(windowState(freshWindow("a",75,age:0,declared:10),providerStatus:"verified",at:now.addingTimeInterval(11)) == .stale)
    precondition(windowState(quota("future",75,observed:now.addingTimeInterval(60),freshUntil:now.addingTimeInterval(3600)),providerStatus:"verified",at:now) == .stale)
    // Web parity: freshnessDeadline rejects observed > now+5000ms only, so +4s is tolerated and +6s is not.
    precondition(windowState(quota("future4",75,observed:now.addingTimeInterval(4),freshUntil:now.addingTimeInterval(3600)),providerStatus:"verified",at:now) == .fresh)
    precondition(windowState(quota("future6",75,observed:now.addingTimeInterval(6),freshUntil:now.addingTimeInterval(3600)),providerStatus:"verified",at:now) == .stale)
    precondition(windowState(quota("bad",75,observed:nil,freshUntil:now.addingTimeInterval(3600)),providerStatus:"verified",at:now) == .stale)
    var malformedObserved=freshWindow("bad2",75);malformedObserved=Quota(id:malformedObserved.id,label:malformedObserved.label,remaining_pct:75,last_known_remaining_pct:nil,resets_at:malformedObserved.resets_at,fresh_until:malformedObserved.fresh_until,observed_at:"not-a-date",status:"fresh",velocity:malformedObserved.velocity)
    precondition(windowState(malformedObserved,providerStatus:"verified",at:now) == .stale)
    precondition(windowState(freshWindow("policy",75,age:11),providerStatus:"verified",maxAgeSeconds:10,at:now) == .stale)
    precondition(windowState(freshWindow("cap",75,age:91),providerStatus:"verified",maxAgeSeconds:1000,at:now) == .stale)
    precondition(windowState(freshWindow("reset",75,reset:-1),providerStatus:"verified",at:now) == .expired)
    precondition(windowState(freshWindow("degraded",75),providerStatus:"error",at:now) == .stale)
    // Selection: exhausted, then earliest valid runout, then minimum numeric; finite before unlimited.
    precondition(currentRemaining(provider("verified",[freshWindow("fresh",75)]),at:now)==75)
    precondition(currentRemaining(provider("verified",[freshWindow("expired-age",0,age:120)]),at:now)==nil)
    precondition(currentRemaining(provider("error",[freshWindow("fresh",75)]),at:now)==nil)
    let riskLate=freshWindow("risk-late",40);let riskEarly=freshWindow("risk-early",60)
    func risk(_ base:Quota,eta:TimeInterval,valid:Bool=true)->Quota {
        Quota(id:base.id,label:base.label,remaining_pct:base.remaining_pct,last_known_remaining_pct:nil,resets_at:base.resets_at,fresh_until:base.fresh_until,observed_at:base.observed_at,status:base.status,velocity:Pace(pct_per_hour:5,confidence:valid ? "observed" : "insufficient",exhausts_before_reset:true,eta_exhaustion_at:stamp.string(from:now.addingTimeInterval(eta))))
    }
    precondition(selection(provider("verified",[risk(riskLate,eta:2700),risk(riskEarly,eta:900),freshWindow("low",10)]),at:now)?.window.id=="risk-early")
    precondition(selection(provider("verified",[freshWindow("exhausted",0),risk(riskEarly,eta:900)]),at:now)?.window.id=="exhausted")
    precondition(selection(provider("verified",[risk(riskEarly,eta:-10),freshWindow("low",10)]),at:now)?.window.id=="low")
    precondition(selection(provider("verified",[risk(riskEarly,eta:2700,valid:false),freshWindow("low",10)]),at:now)?.window.id=="low")
    let unlimitedWindow=quota("cap",nil,status:"unlimited",observed:now,freshUntil:now.addingTimeInterval(3600))
    precondition(selection(provider("verified",[unlimitedWindow,freshWindow("finite",80)]),at:now)?.window.id=="finite")
    precondition(selection(provider("verified",[unlimitedWindow]),at:now)?.state == .unlimited)
    precondition(currentRemaining(provider("verified",[unlimitedWindow]),at:now)==nil && isUnlimited(provider("verified",[unlimitedWindow]),at:now))
    // Last-known history is labelled by callers and never becomes a current value.
    let staleWindow=quota("history",nil,status:"stale",observed:now.addingTimeInterval(-3600),lastKnown:42)
    precondition(currentRemaining(provider("verified",[staleWindow]),at:now)==nil)
    precondition(lastKnownRemaining(provider("verified",[staleWindow]),at:now)==42)
    precondition(selection(provider("verified",[staleWindow,freshWindow("current",55)],"history"),at:now)?.window.id=="current")
    precondition(currentRemaining(provider("verified",[freshWindow("invalid",101)]),at:now)==nil)
    // Fingerprint: bundled apps fail closed without a build fingerprint; a bare --check binary may not.
    let bound=Snapshot(updated_at:"",providers:[],service:ServiceIdentity(name:"headroom",api_version:1,source_fingerprint:"expected"),freshness_policy:nil)
    precondition(compatible(bound,expected:"expected",bundled:true));precondition(!compatible(bound,expected:"other",bundled:true))
    precondition(!compatible(Snapshot(updated_at:"",providers:[],service:nil,freshness_policy:nil),expected:"expected",bundled:true))
    precondition(!compatible(Snapshot(updated_at:"",providers:[],service:nil,freshness_policy:nil),expected:nil,bundled:true))
    precondition(!compatible(Snapshot(updated_at:"",providers:[],service:nil,freshness_policy:nil),expected:nil,bundled:false))
    precondition(compatible(bound,expected:nil,bundled:false))
    // Loss-tolerant bounded decode: bad provider/window entries drop out, good entries survive.
    let json="{\"updated_at\":\"x\",\"freshness_policy\":{\"max_age_seconds\":90},\"providers\":[{\"id\":\"a\",\"name\":\"A\",\"status\":\"verified\",\"plan\":{},\"windows\":[42,{\"id\":\"w\",\"label\":\"W\",\"remaining_pct\":33,\"status\":\"fresh\",\"observed_at\":\""+stamp.string(from:now)+"\",\"velocity\":7}]},\"bad\",{\"id\":\"b\",\"name\":\"B\",\"status\":\"verified\",\"plan\":{},\"windows\":[]}]}"
    let parsed=parseSnapshot(Data(json.utf8));precondition(parsed?.providers.count==2 && parsed?.providers.first?.windows.count==1)
    precondition(parseSnapshot(Data(repeating:0x20,count:maxResponseBytes+1))==nil)
    // A huge api_version must not trap Int conversion; it simply yields no qualified service.
    let hugeVersion="{\"updated_at\":\"x\",\"service\":{\"name\":\"headroom\",\"api_version\":1e300,\"source_fingerprint\":\"x\"},\"providers\":[]}"
    let hugeParsed=parseSnapshot(Data(hugeVersion.utf8));precondition(hugeParsed != nil && hugeParsed?.service == nil && !compatible(hugeParsed!))
    // Frame clamping keeps the full panel inside the chosen visible frame.
    let clamped=clampedFrame(NSRect(x:950,y:-20,width:320,height:300),into:NSRect(x:0,y:0,width:1000,height:800))
    precondition(clamped.minX==680 && clamped.minY==0 && clamped.maxX<=1000 && clamped.maxY<=800)
    print("Headroom native freshness, selection, unlimited, history, decode, fingerprint, and frame checks passed.");exit(0)
}

if CommandLine.arguments.contains("--check") {
    let transport=BoundedLoader();let semaphore=DispatchSemaphore(value:0);var valid=false
    _=transport.load(endpoint){data,response,error in
        defer{semaphore.signal()}
        guard error==nil else{print("Headroom native check: local service connection unavailable.");return}
        guard (response as? HTTPURLResponse)?.statusCode==200,let data else{print("Headroom native check: local service response unavailable.");return}
        guard let value=parseSnapshot(data) else{print("Headroom native check: local snapshot schema mismatch.");return}
        print("Headroom native decoder: \(value.providers.count) plans, \(value.providers.filter{measured($0,maxAgeSeconds:value.freshness_policy?.max_age_seconds)}.count) measured")
        valid = !value.providers.isEmpty && compatible(value)
    }
    let deadline=Date().addingTimeInterval(8);var complete=false
    while Date()<deadline {
        if semaphore.wait(timeout:.now()) == .success{complete=true;break}
        RunLoop.current.run(until:Date().addingTimeInterval(0.05))
    }
    exit(complete && valid ? 0 : 1)
}
private let app=NSApplication.shared
private let delegate=Menu()
app.setActivationPolicy(.accessory);app.delegate=delegate;app.run()
