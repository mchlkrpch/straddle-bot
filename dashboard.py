"""Local task 3 dashboard. Run with Python 3.10+ from a fresh checkout."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qsl, urlsplit
import webbrowser

from reproduce import ROOT, TASK, prepare_engine


class Dashboard(BaseHTTPRequestHandler):
    def respond(self, status, mime, body):
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path == '/':
            self.respond(200, 'text/html', (TASK/'ui/index.html').read_bytes())
        elif route.path == '/comparison.csv':
            self.respond(200, 'text/csv', (TASK/'expected/comparison_daily.csv').read_bytes())
        elif route.path == '/calculate':
            try:
                data = self.server.calculate(dict(parse_qsl(route.query, keep_blank_values=True)))
                body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            except (ValueError, ArithmeticError) as error:
                self.respond(400, 'text/plain', str(error).encode())
                return
            self.respond(200, 'application/json', body)
        else:
            self.send_error(404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true', help='Open the visualizer in the default browser.')
    parser.add_argument('--export-charts', action='store_true', help='Write the three baseline figures and exit.')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='btc-dashboard-') as tmp:
        engine = prepare_engine(tmp)
        sys.path.insert(0, str(engine))
        from research import run_market
        from task3.ui.experiment import calculate
        market = run_market()
        if args.export_charts:
            folder = ROOT/'report/cycles'
            folder.mkdir(exist_ok=True)
            for case in calculate(market, {}, charts_only=True):
                path = folder/(case['key'] + '.svg')
                path.write_text(case['svg'] + '\n', encoding='utf-8')
                print(path)
            return
        with HTTPServer(('127.0.0.1', args.port), Dashboard) as server:
            server.calculate = lambda query: calculate(market, query)
            url = f'http://127.0.0.1:{server.server_port}/'
            print(url, flush=True)
            if args.open:
                Thread(target=webbrowser.open_new_tab, args=(url,), daemon=True).start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == '__main__':
    main()
