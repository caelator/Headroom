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

private func fresh(_ w:Quota)->Bool {
    guard w.status=="fresh",let until=w.fresh_until.flatMap(parsedDate),until>Date() else{return false}
    return w.resets_at.flatMap(parsedDate).map{$0>Date()} ?? true
}

private func measured(_ p:Provider)->Bool {
    p.status != "stale" && p.status != "error" && p.status != "auth_required" &&
      p.windows.contains{fresh($0) && $0.remaining_pct != nil} && p.remaining_pct != nil
}

private final class Menu:NSObject,NSApplicationDelegate,NSMenuDelegate {
    private var status:NSStatusItem!;private let menu=NSMenu()
    private let transport=session();private var snapshot:Snapshot?
    private var fetching=false;private var failed=false;private var fetchedAt:Date?
    private var timer:Timer?;private var expiryTimer:Timer?;private var task:URLSessionDataTask?
    func applicationDidFinishLaunching(_ n:Notification) {
        status=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        status.button?.image=NSImage(systemSymbolName:"gauge.with.dots.needle.67percent",accessibilityDescription:"AI plans")
        status.button?.imagePosition = .imageLeading;status.button?.title="Plans …"
        menu.autoenablesItems=false;menu.delegate=self;status.menu=menu
        poll();timer=Timer.scheduledTimer(withTimeInterval:10,repeats:true){[weak self] _ in self?.poll()}
        expiryTimer=Timer.scheduledTimer(withTimeInterval:1,repeats:true){[weak self] _ in self?.update()}
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
        let candidates=(snapshot?.providers ?? []).filter(measured).sorted{($0.remaining_pct ?? 101)<($1.remaining_pct ?? 101)}
        if let p=candidates.first,!stale {
            let short=p.id=="codex" ? "Codex" : p.id=="antigravity" ? "AG" : p.name
            button.title=short+" "+percent(p.remaining_pct)
            button.toolTip="Headroom · \(p.name): \(percent(p.remaining_pct)) remaining in its limiting quota window. \(candidates.count) of \(snapshot?.providers.count ?? 0) plans have current quota data."
        }else{button.title="Plans —";button.toolTip="Headroom · quota data unavailable; click for connection details."}
        button.setAccessibilityLabel(button.toolTip ?? "Headroom plans")
        let root=FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/aiusage/native-status.json")
        let evidence:[String:Any]=["updated_at":Date().timeIntervalSince1970,"title":button.title,"visible":status.isVisible,
          "stale":stale,"providers":snapshot?.providers.count ?? 0,"measured_providers":candidates.count]
        if let bytes=try? JSONSerialization.data(withJSONObject:evidence){try? bytes.write(to:root,options:.atomic)}
    }
    private func line(_ text:String,into target:NSMenu?=nil) {
        let row=NSMenuItem(title:text,action:nil,keyEquivalent:"");row.isEnabled=false;(target ?? menu).addItem(row)
    }
    func menuWillOpen(_ m:NSMenu) {
        update();menu.removeAllItems();line("Headroom · plan allowances")
        if stale{line("Last-known data · refreshing connection")}
        for p in snapshot?.providers ?? [] {
            let row=NSMenuItem(title:p.name+" · "+(measured(p) && !stale ? percent(p.remaining_pct)+" remaining" : "quota not current"),action:nil,keyEquivalent:"")
            row.isEnabled=true;let sub=NSMenu();line(p.plan.verified_tier ?? "Plan tier not verified",into:sub)
            for w in p.windows {
                line(w.label+": "+(fresh(w) && !stale ? percent(w.remaining_pct)+" remaining" : "not current ("+w.status+")"),into:sub)
                if fresh(w),w.velocity.confidence=="observed",let rate=w.velocity.pct_per_hour,!stale {
                    line(String(format:"Observed net burn: %.2f percentage points/hour",rate),into:sub)
                }
                if let reset=w.resets_at,let date=parsedDate(reset){line("Reset: "+date.formatted(date:.abbreviated,time:.shortened),into:sub)}
                if fresh(w) && w.velocity.exhausts_before_reset==true && !stale{line("⚠ Projected to exhaust before reset",into:sub)}
            }
            if p.windows.isEmpty{line("Quota feed not connected",into:sub)}
            row.submenu=sub;menu.addItem(row)
        }
        menu.addItem(.separator());line("Each plan has independent limits; percentages are not combined.")
        for (label,action) in [("Open plan dashboard",#selector(openDashboard)),("Refresh",#selector(refresh)),("Quit menu indicator",#selector(quit))] {
            let row=NSMenuItem(title:label,action:action,keyEquivalent:"");row.target=self;row.isEnabled=true;menu.addItem(row)
        }
    }
    @objc private func openDashboard(){NSWorkspace.shared.open(dashboard)}
    @objc private func refresh(){poll()}
    @objc private func quit(){timer?.invalidate();expiryTimer?.invalidate();task?.cancel();NSApplication.shared.terminate(nil)}
}

if CommandLine.arguments.contains("--check") {
    let transport=session();let semaphore=DispatchSemaphore(value:0);var valid=false
    transport.dataTask(with:endpoint){data,response,error in
        defer{semaphore.signal()}
        guard error==nil,(response as? HTTPURLResponse)?.statusCode==200,let data,
          let value=try? JSONDecoder().decode(Snapshot.self,from:data) else{return}
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
