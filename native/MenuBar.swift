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
                         status:jsonString(window["status"]) ?? "unknown",velocity:pace)
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
        let task=urlSession.dataTask(with:url)
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

private final class FloatingWidget:NSObject,NSWindowDelegate {
    private let panel=NSPanel(contentRect:NSRect(x:40,y:80,width:320,height:320),
      styleMask:[.borderless,.nonactivatingPanel],backing:.buffered,defer:false)
    private let headline=NSTextField(labelWithString:"Connecting…")
    private let subtitle=NSTextField(labelWithString:"Waiting for current quota")
    private let footnote=NSTextField(labelWithString:"Separate plan allowances")
    private let rowsStack=NSStackView()
    private var rows:[String:(NSTextField,NSProgressIndicator)]=[:]
    private var rowSignature:[String]=[]
    private var screenObserver:NSObjectProtocol?
    private let open:()->Void
    private let defaults=UserDefaults.standard
    var isVisible:Bool {panel.isVisible}
    var panelVisible:Bool {panel.isVisible}

    init(openDashboard:@escaping ()->Void,testing:Bool=false) {
        self.open=openDashboard;super.init()
        if !testing {defaults.register(defaults:["floatingWidgetEnabled":true])}
        if !testing, defaults.object(forKey:"NSWindow Frame HeadroomFloatingWidget")==nil,
           let frame=UserDefaults(suiteName:"com.xxx.codex-usage-menu")?.string(forKey:"NSWindow Frame HeadroomFloatingWidget") {
            defaults.set(frame,forKey:"NSWindow Frame HeadroomFloatingWidget")
        }
        panel.title="Headroom floating widget";panel.level = .floating
        panel.collectionBehavior=[.canJoinAllSpaces,.fullScreenAuxiliary]
        panel.isOpaque=false;panel.backgroundColor = .clear;panel.hasShadow=true
        panel.isFloatingPanel=true;panel.hidesOnDeactivate=false
        panel.becomesKeyOnlyIfNeeded=true;panel.isMovableByWindowBackground=true
        panel.isReleasedWhenClosed=false;panel.delegate=self
        let surface=NSVisualEffectView(frame:NSRect(x:0,y:0,width:320,height:320))
        surface.material = .hudWindow;surface.blendingMode = .behindWindow;surface.state = .active
        surface.wantsLayer=true;surface.layer?.cornerRadius=18;surface.layer?.masksToBounds=true
        panel.contentView=surface
        let stack=NSStackView();stack.orientation = .vertical;stack.alignment = .leading;stack.spacing=8
        stack.translatesAutoresizingMaskIntoConstraints=false;surface.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo:surface.leadingAnchor,constant:16),
          stack.trailingAnchor.constraint(equalTo:surface.trailingAnchor,constant:-16),
          stack.topAnchor.constraint(equalTo:surface.topAnchor,constant:14),
          stack.bottomAnchor.constraint(equalTo:surface.bottomAnchor,constant:-14)])
        let title=NSTextField(labelWithString:"◉  Headroom");title.font = .systemFont(ofSize:13,weight:.semibold)
        let hide=NSButton(title:"Hide",target:self,action:#selector(hideWidget));hide.bezelStyle = .inline
        hide.setAccessibilityLabel("Hide floating widget")
        let header=NSStackView(views:[title,spacer(),hide]);header.orientation = .horizontal
        stack.addArrangedSubview(header)
        headline.font = .monospacedDigitSystemFont(ofSize:27,weight:.semibold)
        headline.lineBreakMode = .byTruncatingTail;headline.setAccessibilityLabel("Limiting current quota")
        subtitle.font = .systemFont(ofSize:11);subtitle.textColor = .secondaryLabelColor
        stack.addArrangedSubview(headline);stack.addArrangedSubview(subtitle)
        rowsStack.orientation = .vertical;rowsStack.alignment = .leading;rowsStack.spacing=8
        stack.addArrangedSubview(rowsStack)
        footnote.font = .systemFont(ofSize:10);footnote.textColor = .secondaryLabelColor
        stack.addArrangedSubview(footnote)
        let button=NSButton(title:"Open dashboard",target:self,action:#selector(FloatingWidget.openDashboard))
        button.bezelStyle = .rounded;button.setAccessibilityLabel("Open Headroom dashboard")
        stack.addArrangedSubview(button)
        for view in stack.arrangedSubviews {view.widthAnchor.constraint(equalTo:stack.widthAnchor).isActive=true}
        rowsStack.widthAnchor.constraint(equalTo:stack.widthAnchor).isActive=true
        if !testing {
            panel.setFrameAutosaveName("HeadroomFloatingWidget")
            if !panel.setFrameUsingName("HeadroomFloatingWidget") {placeOnScreen()}
        }
        clampPanelFrame()
        screenObserver=NotificationCenter.default.addObserver(forName:NSApplication.didChangeScreenParametersNotification,object:nil,queue:.main){[weak self] _ in self?.clampPanelFrame()}
        if !testing && defaults.bool(forKey:"floatingWidgetEnabled") {panel.orderFrontRegardless()}
    }
    deinit {if let screenObserver {NotificationCenter.default.removeObserver(screenObserver)}}

    private func spacer()->NSView {
        let view=NSView();view.setContentHuggingPriority(.defaultLow,for:.horizontal);return view
    }
    private func placeOnScreen() {
        guard let frame=NSScreen.main?.visibleFrame else{return}
        panel.setFrameOrigin(NSPoint(x:frame.maxX-panel.frame.width-24,y:frame.minY+24))
    }
    func clampPanelFrame() {
        guard !NSScreen.screens.isEmpty else{return}
        let frame=panel.frame
        let target=NSScreen.screens.max(by:{
            $0.visibleFrame.intersection(frame).area < $1.visibleFrame.intersection(frame).area
        }) ?? NSScreen.main ?? NSScreen.screens[0]
        panel.setFrame(clampedFrame(frame,into:target.visibleFrame),display:true)
    }
    private func rebuildRows(_ providers:[Provider]) {
        let signature=providers.map{$0.id+"\u{1f}"+$0.name}
        guard signature != rowSignature else{return}
        rowSignature=signature;rows.removeAll()
        for view in rowsStack.arrangedSubviews {rowsStack.removeArrangedSubview(view);view.removeFromSuperview()}
        for provider in providers {
            let label=NSTextField(labelWithString:provider.name);label.font = .systemFont(ofSize:11,weight:.medium)
            let value=NSTextField(labelWithString:"—");value.font = .monospacedDigitSystemFont(ofSize:11,weight:.medium)
            value.alignment = .right;value.widthAnchor.constraint(greaterThanOrEqualToConstant:104).isActive=true
            let progress=NSProgressIndicator();progress.style = .bar;progress.isIndeterminate=false
            progress.minValue=0;progress.maxValue=100;progress.doubleValue=0
            progress.widthAnchor.constraint(equalToConstant:58).isActive=true
            progress.setAccessibilityLabel(provider.name+" quota remaining")
            let row=NSStackView(views:[label,spacer(),progress,value]);row.orientation = .horizontal;row.spacing=8
            rowsStack.addArrangedSubview(row);rows[provider.id]=(value,progress)
            row.widthAnchor.constraint(equalTo:rowsStack.widthAnchor).isActive=true
        }
        // Keep every API row reachable: grow/shrink with the provider list, then clamp on screen.
        let desired=CGFloat(max(280,196+providers.count*28))
        if abs(panel.frame.height-desired)>1 {
            var frame=panel.frame;let top=frame.maxY;frame.size.height=desired;frame.origin.y=top-desired
            panel.setFrame(frame,display:true)
        }
        clampPanelFrame()
    }
    func setVisible(_ visible:Bool) {
        defaults.set(visible,forKey:"floatingWidgetEnabled")
        if visible {clampPanelFrame();panel.orderFrontRegardless()} else {panel.orderOut(nil)}
    }
    func update(_ providers:[Provider],stale:Bool,maxAgeSeconds:Double?=nil,at now:Date=Date()) {
        rebuildRows(providers)
        let evaluated=providers.enumerated().map{(index:$0.offset,provider:$0.element,selection:selection($0.element,providerIndex:$0.offset,maxAgeSeconds:maxAgeSeconds,at:now))}
        let finite=evaluated.filter{!stale && $0.selection?.state == .fresh}
            .sorted{(validPercent($0.selection?.window.remaining_pct) ?? 101) < (validPercent($1.selection?.window.remaining_pct) ?? 101)}
        let unlimited=evaluated.filter{!stale && $0.selection?.state == .unlimited}
        if let entry=finite.first,let selected=entry.selection,let remaining=validPercent(selected.window.remaining_pct) {
            let name=entry.provider.id=="codex" ? "Codex" : entry.provider.name
            headline.stringValue=name+" "+percent(remaining)
            headline.textColor=remaining<=10 ? .systemRed : .labelColor
            subtitle.stringValue="Current remaining · most constrained plan"
        }else if let entry=unlimited.first {
            headline.stringValue=entry.provider.name+" Unlimited";headline.textColor = .labelColor
            subtitle.stringValue="Current · no quota cap reported"
        }else if !stale, let entry=evaluated.first(where:{lastKnownRemaining($0.provider,maxAgeSeconds:maxAgeSeconds,at:now) != nil}),
                  let known=lastKnownRemaining(entry.provider,maxAgeSeconds:maxAgeSeconds,at:now) {
            headline.stringValue=percent(known);headline.textColor = .secondaryLabelColor
            subtitle.stringValue="Last known · not current ("+entry.provider.name+")"
        }else{
            headline.stringValue=providers.isEmpty ? "Connecting…" : "Quota unavailable"
            headline.textColor = .secondaryLabelColor;subtitle.stringValue="Waiting for a fresh provider reading"
        }
        for entry in evaluated {
            guard let (value,progress)=rows[entry.provider.id] else{continue}
            let provider=entry.provider
            let remaining=stale ? nil : (entry.selection?.state == .fresh ? validPercent(entry.selection?.window.remaining_pct) : nil)
            let unlimitedNow = !stale && entry.selection?.state == .unlimited
            let known=stale ? nil : lastKnownRemaining(provider,maxAgeSeconds:maxAgeSeconds,at:now)
            if let remaining {
                value.stringValue=percent(remaining);value.textColor=remaining<=10 ? .systemRed : .labelColor
                value.setAccessibilityLabel(provider.name+": "+percent(remaining)+" remaining")
                progress.doubleValue=remaining;progress.alphaValue=1
                progress.setAccessibilityValue(percent(remaining)+" remaining")
            }else if unlimitedNow {
                value.stringValue="Unlimited";value.textColor = .labelColor
                value.setAccessibilityLabel(provider.name+": unlimited, no quota cap reported")
                progress.doubleValue=100;progress.alphaValue=1;progress.setAccessibilityValue("unlimited")
            }else if let known {
                // Labelled history only: never draw a historical meter.
                value.stringValue="Last known "+percent(known);value.textColor = .secondaryLabelColor
                value.setAccessibilityLabel(provider.name+": last known "+percent(known)+", not current")
                progress.doubleValue=0;progress.alphaValue=0.25;progress.setAccessibilityValue("not current; last known "+percent(known))
            }else{
                value.stringValue=provider.status=="auth_required" ? "Sign in" : "—"
                value.textColor = .secondaryLabelColor
                value.setAccessibilityLabel(provider.name+": quota not current")
                progress.doubleValue=0;progress.alphaValue=0.25;progress.setAccessibilityValue("unavailable")
            }
        }
        let currentCount=finite.count+unlimited.count
        footnote.stringValue="\(currentCount) of \(providers.count) current · separate allowances"
    }
    func windowShouldClose(_ sender:NSWindow)->Bool {setVisible(false);return false}
    func assertLayoutRows(_ expected:Int) {
        panel.contentView?.layoutSubtreeIfNeeded()
        precondition(rowsStack.arrangedSubviews.count==expected && rows.count==expected)
    }
    @objc private func hideWidget(){setVisible(false)}
    @objc private func openDashboard(){open()}
}

private extension NSRect {var area:CGFloat {isNull || isEmpty ? 0 : width*height}}

private final class Menu:NSObject,NSApplicationDelegate,NSMenuDelegate {
    private var status:NSStatusItem!;private let menu=NSMenu()
    private let transport=BoundedLoader();private var snapshot:Snapshot?
    private var fetching=false;private var failed=false;private var fetchedAt:Date?
    private var timer:Timer?;private var expiryTimer:Timer?;private var task:URLSessionDataTask?
    private var dashboardTask:URLSessionDataTask?;private var openingDashboard=false
    private var widget:FloatingWidget?
    private var lastEvidenceSignature:String?;private var lastEvidenceWrite:Date?
    func applicationDidFinishLaunching(_ n:Notification) {
        status=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        status.button?.image=NSImage(systemSymbolName:"gauge.with.dots.needle.67percent",accessibilityDescription:"AI plans")
        status.button?.imagePosition = .imageLeading;status.button?.title="Plans …"
        menu.autoenablesItems=false;menu.delegate=self;status.menu=menu
        widget=FloatingWidget(openDashboard:{[weak self] in self?.openDashboard()})
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
        for (label,action) in [(widgetLabel,#selector(toggleWidget)),("Open plan dashboard",#selector(openDashboard)),("Refresh",#selector(refresh)),("Quit menu indicator",#selector(quit))] {
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
    @objc private func refresh(){poll()}
    @objc private func quit(){timer?.invalidate();expiryTimer?.invalidate();task?.cancel();dashboardTask?.cancel();NSApplication.shared.terminate(nil)}
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
    print("Headroom native widget layout and dynamic row checks passed.");exit(0)
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
