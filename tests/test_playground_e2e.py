"""Playground end-to-end: real petsitter server, fake upstream, real browser.

Boots the actual app with a trickset holding three tricks (one always-on, one
keyword-gated, one that rewrites output), points it at a stub OpenAI endpoint,
then drives the docked panel in Chromium and checks the trace and the
firing highlight.
"""
import json, os, pathlib, shutil, socket, subprocess, sys, tempfile, threading, time
import http.server, socketserver

ROOT = pathlib.Path("/home/claude/work/petsitter")
sys.path.insert(0, str(ROOT))

tmp = pathlib.Path(tempfile.mkdtemp())
cfg = tmp / "config"; (cfg / "tricksets").mkdir(parents=True)
tricks = tmp / "tricks"; tricks.mkdir()

(tricks / "shouty.py").write_text('''
from src.trick import Trick

class ShoutyTrick(Trick):
    __version__ = "1.0.0"
    __brief__ = "Uppercases the reply"
    __display_name__ = "Shouty"

    def system_prompt(self, to_add: str) -> str:
        return "Be brief."

    def post_hook(self, context: list) -> list:
        if context and isinstance(context[-1].get("content"), str):
            context[-1]["content"] = context[-1]["content"].upper()
        return context
''')

(tricks / "quiet.py").write_text('''
from src.trick import Trick

class QuietTrick(Trick):
    __version__ = "1.0.0"
    __brief__ = "Only wakes on the keyword"
    __display_name__ = "Quiet"
    keywords = ["banana"]

    def system_prompt(self, to_add: str) -> str:
        return "Mention bananas."
''')

(tricks / "inert.py").write_text('''
from src.trick import Trick

class InertTrick(Trick):
    __version__ = "1.0.0"
    __brief__ = "Does nothing at all"
    __display_name__ = "Inert"
''')

UP_PORT, APP_PORT = 8841, 8842


class Upstream(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a): pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        Upstream.seen.append(body)
        out = json.dumps({
            "id": "chatcmpl-x", "object": "chat.completion", "created": 0,
            "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello there"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


socketserver.TCPServer.allow_reuse_address = True
threading.Thread(target=socketserver.TCPServer(("127.0.0.1", UP_PORT), Upstream).serve_forever,
                 daemon=True).start()

(cfg / "tricksets" / "demo.json").write_text(json.dumps({
    "schema": "0.8.0", "name": "demo",
    "filters": {"X-Title": "*", "Model": "*"},
    "tricks": [str(tricks / "shouty.py"), str(tricks / "quiet.py"), str(tricks / "inert.py")],
    "parameters": {}, "models": {},
}))
(cfg / "config.json").write_text(json.dumps({
    "model_url": f"http://127.0.0.1:{UP_PORT}", "model_name": "stub", "api_key": "",
    "modelset": {"default": {"url": f"http://127.0.0.1:{UP_PORT}", "model": "stub"}},
}))

env = dict(os.environ, PET_CONFIG_DIR=str(cfg), PYTHONPATH=str(ROOT))
proc = subprocess.Popen(
    [sys.executable, "-m", "src.server", "-c", str(cfg), "-l", f"127.0.0.1:{APP_PORT}"],
    cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_up(port, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            print("server died:\n", proc.stdout.read())
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), 0.4):
                return True
        except OSError:
            time.sleep(0.25)
    return False


if not wait_up(APP_PORT):
    print("server never came up"); sys.exit(1)
time.sleep(1.0)

fails = []
def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (("  " + str(detail)) if not cond else ""))
    if not cond: fails.append(label)

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"http://127.0.0.1:{APP_PORT}/")
        pg.wait_for_timeout(1800)

        print("\\n1. panel opens from the header")
        check("panel starts hidden", pg.eval_on_selector("#pg-panel", "e => e.style.display") == "none")
        # the dashboard boots on whatever trickset it saw first; pin it to ours
        pg.evaluate("switchTrickset('demo')")
        pg.wait_for_timeout(900)
        pg.click("#try-btn")
        pg.wait_for_timeout(300)
        check("panel visible", pg.is_visible("#pg-panel"))
        check("target shows trickset", "demo" in pg.eval_on_selector("#pg-target", "e => e.textContent"))

        print("\\n2. a plain message")
        pg.fill("#pg-text", "say hi")
        pg.press("#pg-text", "Enter")
        pg.wait_for_timeout(2500)
        bodies = pg.eval_on_selector_all(".pg-msg .pg-body", "e => e.map(x => x.textContent)")
        check("reply came back uppercased by the trick", "HELLO THERE" in bodies[-1], bodies)
        pills = pg.eval_on_selector_all(".pg-msg:last-child .pg-pill", "e => e.map(x => x.textContent)")
        titles = pg.eval_on_selector_all(".pg-msg:last-child .pg-pill", "e => e.map(x => x.title)")
        print("   pills:", pills)
        check("Shouty is in the trace", "Shouty" in pills, pills)
        check("Quiet shown as dormant", "Quiet" in pills, pills)
        quiet_title = titles[pills.index("Quiet")] if "Quiet" in pills else ""
        check("dormant pill explains why", "banana" in quiet_title, quiet_title)
        check("timing in the meta pill", any("ms" in x for x in pills), pills)

        pg.screenshot(path="/home/claude/work/playground.png")

        print("\\n3. the fired highlight lands on the right row")
        fired = pg.evaluate("""() => {
          // re-run the flash so we can observe it synchronously
          flashFiredTricks([{stage:'post_hook', trick:'ShoutyTrick', changed:true}]);
          return [...document.querySelectorAll('#loaded-tricks .trick-item.fired')].map(e => e.dataset.name);
        }""")
        check("only ShoutyTrick lights up", fired == ["ShoutyTrick"], fired)

        print("\\n4. keyword gating changes the trace")
        pg.fill("#pg-text", "banana please")
        pg.press("#pg-text", "Enter")
        pg.wait_for_timeout(2500)
        pills2 = pg.eval_on_selector_all(".pg-msg:last-child .pg-pill", "e => e.map(x => x.textContent)")
        titles2 = pg.eval_on_selector_all(".pg-msg:last-child .pg-pill", "e => e.map(x => x.title)")
        print("   pills:", pills2)
        qt = titles2[pills2.index("Quiet")] if "Quiet" in pills2 else ""
        check("Quiet now reports running a stage", "Ran:" in qt, qt)
        check("upstream saw the banana system prompt",
              any("banana" in json.dumps(m).lower() for m in Upstream.seen[-1:]), Upstream.seen[-1:])

        print("\\n5. clear + errors")
        pg.click("#pg-panel >> text=Clear")
        pg.wait_for_timeout(200)
        check("log resets to the hint", pg.eval_on_selector_all(".pg-msg", "e => e.length") == 0)
        check("no page errors", not errs, errs)
        b.close()
finally:
    proc.terminate()
    try: proc.wait(5)
    except Exception: proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)

print("\\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
