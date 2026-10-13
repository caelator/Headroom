import Cocoa
import Foundation

private let endpoint=URL(string:"http://127.0.0.1:8793/api/plans")!
private let dashboard=URL(string:"http://127.0.0.1:8793/")!

private struct Plan:Decodable {let verified_tier:String?}
private struct Pace:Decodable {let pct_per_hour:Double?;let confidence:String;let exhausts_before_reset:Bool?}
private struct Quota:Decodable {
    let id:String;let label:String;let remaining_pct:Double?;let resets_at:String?;let fresh_until:String?;let status:String;let velocity:Pace
}
private struct Provider:Decodable {
    let id:String;let name:String;let status:String;let remaining_pct:Double?;let plan:Plan;let windows:[Quota]
    let limiting_window_id:String?
}
private struct Snapshot:Decodable {let updated_at:String;let providers:[Provider]}

private func session()->URLSession {
    let c=URLSessionConfiguration.ephemeral;c.connectionProxyDictionary=[:]
    c.timeoutIntervalForRequest=4;c.timeoutIntervalForResource=5
    c.waitsForConnectivity=false;c.urlCache=nil;c.httpCookieStorage=nil
    return URLSession(configuration:c)
}

private func percent(_ value:Double?)->String {
    guard let value,value.isFinite,value>=0,value<=100 else{return "unknown"}
    return String(format:value<1 ? "%.2f%%" : "%.1f%%",value)
}

private func parsedDate(_ text:String)->Date? {
    let f=ISO8601DateFormatter();f.formatOptions=[.withInternetDateTime,.withFractionalSeconds]
    return f.date(from:text) ?? ISO8601DateFormatter().date(from:text)
}

private func fresh(_ w:Quota,at now:Date=Date())->Bool {
    guard w.status=="fresh",let until=w.fresh_until.flatMap(parsedDate),until>now else{return false}
    return w.resets_at.flatMap(parsedDate).map{$0>now} ?? true
}

private func currentRemaining(_ p:Provider,at now:Date=Date())->Double? {
    guard ["verified","partial"].contains(p.status) else{return nil}
    let windows=p.windows.filter { window in
        guard fresh(window,at:now),let value=window.remaining_pct else{return false}
        return value.isFinite && value>=0 && value<=100
    }
    if let selected=windows.first(where:{$0.id==p.limiting_window_id}) {return selected.remaining_pct}
    return windows.compactMap(\.remaining_pct).min()
}

private func measured(_ p:Provider)->Bool {currentRemaining(p) != nil}

private final class FloatingWidget:NSObject,NSWindowDelegate {
    private let panel=NSPanel(contentRect:NSRect(x:40,y:80,width:320,height:320),
      styleMask:[.borderless,.nonactivatingPanel],backing:.buffered,defer:false)
    private let headline=NSTextField(labelWithString:"Connecting…")
    private let subtitle=NSTextField(labelWithString:"Waiting for current quota")
    private let footnote=NSTextField(labelWithString:"Separate plan allowances")
    private var rows:[String:(NSTextField,NSProgressIndicator)]=[:]
    private let open:()->Void
    private let defaults=UserDefaults.standard
    var isVisible:Bool {panel.isVisible}
    private let names:[(String,String)]=[("codex","Codex / ChatGPT"),("muse","Muse"),("zai","z.ai"),
      ("cursor","Cursor"),("antigravity","Antigravity"),("claude","Claude")]

    init(openDashboard:@escaping ()->Void) {
        self.open=openDashboard;super.init()
        defaults.register(defaults:["floatingWidgetEnabled":true])
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
        for (id,name) in names {
            let label=NSTextField(labelWithString:name);label.font = .systemFont(ofSize:11,weight:.medium)
            let value=NSTextField(labelWithString:"—");value.font = .monospacedDigitSystemFont(ofSize:11,weight:.medium)
            value.alignment = .right;value.widthAnchor.constraint(greaterThanOrEqualToConstant:70).isActive=true
            let progress=NSProgressIndicator();progress.style = .bar;progress.isIndeterminate=false
            progress.minValue=0;progress.maxValue=100;progress.doubleValue=0
            progress.widthAnchor.constraint(equalToConstant:58).isActive=true
            progress.setAccessibilityLabel(name+" quota remaining")
            let row=NSStackView(views:[label,spacer(),progress,value]);row.orientation = .horizontal;row.spacing=8
            stack.addArrangedSubview(row);rows[id]=(value,progress)
        }
        footnote.font = .systemFont(ofSize:10);footnote.textColor = .secondaryLabelColor
        stack.addArrangedSubview(footnote)
        let button=NSButton(title:"Open dashboard",target:self,action:#selector(FloatingWidget.openDashboard))
        button.bezelStyle = .rounded;button.setAccessibilityLabel("Open Headroom dashboard")
        stack.addArrangedSubview(button)
        for view in stack.arrangedSubviews {view.widthAnchor.constraint(equalTo:stack.widthAnchor).isActive=true}
        panel.setFrameAutosaveName("HeadroomFloatingWidget")
        if !panel.setFrameUsingName("HeadroomFloatingWidget") {placeOnScreen()}
        if !NSScreen.screens.contains(where:{$0.visibleFrame.intersects(panel.frame)}) {placeOnScreen()}
        if defaults.bool(forKey:"floatingWidgetEnabled") {panel.orderFrontRegardless()}
    }

    private func spacer()->NSView {
        let view=NSView();view.setContentHuggingPriority(.defaultLow,for:.horizontal);return view
    }
    private func placeOnScreen() {
        guard let frame=NSScreen.main?.visibleFrame else{return}
        panel.setFrameOrigin(NSPoint(x:frame.maxX-panel.frame.width-24,y:frame.minY+24))
    }
    func setVisible(_ visible:Bool) {
        defaults.set(visible,forKey:"floatingWidgetEnabled")
        if visible {panel.orderFrontRegardless()} else {panel.orderOut(nil)}
    }
    func update(_ providers:[Provider],stale:Bool) {
        let candidates=stale ? [] : providers.filter(measured).sorted{(currentRemaining($0) ?? 101)<(currentRemaining($1) ?? 101)}
        if let provider=candidates.first,let remaining=currentRemaining(provider) {
            let name=provider.id=="codex" ? "Codex" : provider.name
            headline.stringValue=name+" "+percent(remaining)
            headline.textColor=remaining<=10 ? .systemRed : .labelColor
            subtitle.stringValue="Current remaining · most constrained plan"
        }else{
            headline.stringValue=providers.isEmpty ? "Connecting…" : "Quota unavailable"
            headline.textColor = .secondaryLabelColor;subtitle.stringValue="Waiting for a fresh provider reading"
        }
        for (id,name) in names {
            guard let (value,progress)=rows[id] else{continue}
            let provider=providers.first(where:{$0.id==id})
            let remaining=stale ? nil : provider.flatMap{currentRemaining($0)}
            value.stringValue=remaining.map{percent($0)} ?? (provider?.status=="auth_required" ? "Sign in" : "—")
            value.textColor=remaining == nil ? .secondaryLabelColor : (remaining!<=10 ? .systemRed : .labelColor)
            value.setAccessibilityLabel(name+": "+(remaining.map{percent($0)+" remaining"} ?? "quota not current"))
            progress.doubleValue=remaining ?? 0;progress.alphaValue=remaining == nil ? 0.25 : 1
            progress.setAccessibilityValue(remaining.map{percent($0)+" remaining"} ?? "unavailable")
        }
        footnote.stringValue="\(candidates.count) of \(names.count) current · separate allowances"
    }
    func windowShouldClose(_ sender:NSWindow)->Bool {setVisible(false);return false}
    @objc private func hideWidget(){setVisible(false)}
    @objc private func openDashboard(){open()}
}

private final class Menu:NSObject,NSApplicationDelegate,NSMenuDelegate {
    private var status:NSStatusItem!;private let menu=NSMenu()
    private let transport=session();private var snapshot:Snapshot?
    private var fetching=false;private var failed=false;private var fetchedAt:Date?
    private var timer:Timer?;private var expiryTimer:Timer?;private var task:URLSessionDataTask?
    private var widget:FloatingWidget?
    func applicationDidFinishLaunching(_ n:Notification) {
        status=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        status.button?.image=NSImage(systemSymbolName:"gauge.with.dots.needle.67percent",accessibilityDescription:"AI plans")
        status.button?.imagePosition = .imageLeading;status.button?.title="Plans …"
        menu.autoenablesItems=false;menu.delegate=self;status.menu=menu
        widget=FloatingWidget(openDashboard:{[weak self] in self?.openDashboard()})
        poll();timer=Timer.scheduledTimer(withTimeInterval:10,repeats:true){[weak self] _ in self?.poll()}
        expiryTimer=Timer.scheduledTimer(withTimeInterval:1,repeats:true){[weak self] _ in self?.update()}
    }
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool {
        widget?.setVisible(true);return false
    }
    private var stale:Bool {failed || (fetchedAt.map{Date().timeIntervalSince($0)>30} ?? true)}
    private func poll() {
        guard !fetching else{return};fetching=true
        task=transport.dataTask(with:endpoint){[weak self] data,response,error in
            let value=data.flatMap{try? JSONDecoder().decode(Snapshot.self,from:$0)}
            DispatchQueue.main.async{
                guard let self else{return};self.fetching=false;self.task=nil
                if error==nil,(response as? HTTPURLResponse)?.statusCode==200,let value {
                    self.snapshot=value;self.fetchedAt=Date();self.failed=false
                }else{self.failed=true}
                self.update()
            }
        };task?.resume()
    }
    private func update() {
        guard let button=status.button else{return}
        let candidates=(snapshot?.providers ?? []).filter(measured).sorted{(currentRemaining($0) ?? 101)<(currentRemaining($1) ?? 101)}
        if let p=candidates.first,!stale {
            let short=p.id=="codex" ? "Codex" : p.id=="antigravity" ? "AG" : p.name
            button.title=short+" "+percent(currentRemaining(p))
            button.toolTip="Headroom · \(p.name): \(percent(currentRemaining(p))) remaining in its current limiting quota window. \(candidates.count) of \(snapshot?.providers.count ?? 0) plans have current quota data."
        }else{button.title="Plans —";button.toolTip="Headroom · quota data unavailable; click for connection details."}
        button.setAccessibilityLabel(button.toolTip ?? "Headroom plans")
        widget?.update(snapshot?.providers ?? [],stale:stale)
        let root=FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/aiusage/native-status.json")
        let evidence:[String:Any]=["updated_at":Date().timeIntervalSince1970,"title":button.title,"visible":status.isVisible,
          "stale":stale,"providers":snapshot?.providers.count ?? 0,"measured_providers":stale ? 0 : candidates.count,"widget_visible":widget?.isVisible ?? false]
        if let bytes=try? JSONSerialization.data(withJSONObject:evidence){try? bytes.write(to:root,options:.atomic)}
    }
    private func line(_ text:String,into target:NSMenu?=nil) {
        let row=NSMenuItem(title:text,action:nil,keyEquivalent:"");row.isEnabled=false;(target ?? menu).addItem(row)
    }
    func menuWillOpen(_ m:NSMenu) {
        update();menu.removeAllItems();line("Headroom · plan allowances")
        if stale{line("Last-known data · refreshing connection")}
        for p in snapshot?.providers ?? [] {
            let providerCurrent=["verified","partial"].contains(p.status) && !stale
            let row=NSMenuItem(title:p.name+" · "+(measured(p) && !stale ? percent(currentRemaining(p))+" remaining" : "quota not current"),action:nil,keyEquivalent:"")
            row.isEnabled=true;let sub=NSMenu();line(p.plan.verified_tier ?? "Plan tier not verified",into:sub)
            for w in p.windows {
                line(w.label+": "+(fresh(w) && providerCurrent ? percent(w.remaining_pct)+" remaining" : "not current ("+w.status+")"),into:sub)
                if fresh(w),w.velocity.confidence=="observed",let rate=w.velocity.pct_per_hour,providerCurrent {
                    line(String(format:"Observed net burn: %.2f percentage points/hour",rate),into:sub)
                }
                if let reset=w.resets_at,let date=parsedDate(reset){line("Reset: "+date.formatted(date:.abbreviated,time:.shortened),into:sub)}
                if fresh(w) && w.velocity.exhausts_before_reset==true && providerCurrent{line("⚠ Projected to exhaust before reset",into:sub)}
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
    @objc private func openDashboard(){NSWorkspace.shared.open(dashboard)}
    @objc private func toggleWidget(){widget?.setVisible(widget?.isVisible != true)}
    @objc private func refresh(){poll()}
    @objc private func quit(){timer?.invalidate();expiryTimer?.invalidate();task?.cancel();NSApplication.shared.terminate(nil)}
}

if CommandLine.arguments.contains("--self-test") {
    let now=Date();let stamp=ISO8601DateFormatter()
    func window(_ id:String,_ value:Double,_ expiry:Date,_ reset:Date?=nil)->Quota {
        Quota(id:id,label:id,remaining_pct:value,resets_at:reset.map{stamp.string(from:$0)},
          fresh_until:stamp.string(from:expiry),status:"fresh",velocity:Pace(pct_per_hour:nil,confidence:"insufficient",exhausts_before_reset:nil))
    }
    let freshWindow=window("fresh",75,now.addingTimeInterval(60))
    let expired=window("expired",0,now.addingTimeInterval(-1))
    let plan=Plan(verified_tier:nil)
    func provider(_ status:String,_ windows:[Quota],_ limiting:String?=nil)->Provider {
        Provider(id:"test",name:"Test",status:status,remaining_pct:0,plan:plan,windows:windows,limiting_window_id:limiting)
    }
    precondition(currentRemaining(provider("verified",[freshWindow]),at:now)==75)
    precondition(currentRemaining(provider("verified",[expired]),at:now)==nil)
    precondition(currentRemaining(provider("verified",[expired,freshWindow],"expired"),at:now)==75)
    precondition(currentRemaining(provider("error",[freshWindow]),at:now)==nil)
    precondition(currentRemaining(provider("unavailable",[freshWindow]),at:now)==nil)
    precondition(currentRemaining(provider("partial",[window("reset",20,now.addingTimeInterval(60),now.addingTimeInterval(-1))]),at:now)==nil)
    precondition(currentRemaining(provider("verified",[window("invalid",101,now.addingTimeInterval(60))]),at:now)==nil)
    print("Headroom native freshness, mixed-window expiry, and failure checks passed.");exit(0)
}

if CommandLine.arguments.contains("--check") {
    let transport=session();let semaphore=DispatchSemaphore(value:0);var valid=false
    transport.dataTask(with:endpoint){data,response,error in
        defer{semaphore.signal()}
        guard error==nil else{print("Headroom native check: local service connection unavailable.");return}
        guard (response as? HTTPURLResponse)?.statusCode==200,let data else{print("Headroom native check: local service response unavailable.");return}
        guard let value=try? JSONDecoder().decode(Snapshot.self,from:data) else{print("Headroom native check: local snapshot schema mismatch.");return}
        print("Headroom native decoder: \(value.providers.count) plans, \(value.providers.filter(measured).count) measured")
        valid=value.providers.count>=6
    }.resume()
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
