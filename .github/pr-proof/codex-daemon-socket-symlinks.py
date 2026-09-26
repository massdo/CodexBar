#!/usr/bin/env python3
"""Compare the original and fixed daemon guard with real temporary Unix sockets.

Run on macOS from this checkout:
    python3 .github/pr-proof/codex-daemon-socket-symlinks.py

Only temporary socket fixtures are created. CLI/process checks are injected;
no authentication file is read and no installed daemon is restarted.
The production restartIfRunning method is copied verbatim from the pinned commits.
"""

import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

repo = Path(__file__).resolve().parents[2]
out = Path(tempfile.mkdtemp(prefix='codex-socket-proof-', dir='/private/tmp'))
base_revision = 'b4f88401a3acfbca133ad584c9c48da48aaf351e'
fixed_revision = '89775a6123716128d344d54d197c93b5fec5c3f9'
source_path = 'Sources/CodexBar/CodexAppServerDaemon.swift'
baseline = subprocess.check_output(['git', 'show', f'{base_revision}:{source_path}'], cwd=repo, text=True)
candidate = subprocess.check_output(['git', 'show', f'{fixed_revision}:{source_path}'], cwd=repo, text=True)

stubs = '''
import Foundation
import Darwin
enum CodexHomeScope {
    static func isAppServerProcess(_ pid: Int32) -> Bool { false }
    static func scopedEnvironment(base: [String: String], codexHome: String) -> [String: String] {
        var env = base
        env["CODEX_HOME"] = codexHome
        return env
    }
}
struct ProofLogger {
    func info(_ message: String) {}
    func warning(_ message: String, metadata: [String: String]) {}
}
enum CodexBarLog { static func logger(_ name: String) -> ProofLogger { ProofLogger() } }
func L(_ message: String) -> String { message }
enum ProofError: Error { case unexpectedRealCommand }
'''

main = '''
@main
struct SocketFixtureProof {
    @MainActor
    static func main() async throws {
        let args = CommandLine.arguments
        let home = URL(fileURLWithPath: args[1])
        let reportedSocket = args[2]
        let expectedRestart = args[3] == "restart"
        var calls: [String] = []
        let daemon = CodexAppServerDaemon(isAppServerProcess: { $0 == 123 }, run: { command, env in
            precondition(env["CODEX_HOME"] == home.resolvingSymlinksInPath().standardizedFileURL.path)
            calls.append(command)
            let data = try JSONSerialization.data(withJSONObject: [
                "status": "running", "backend": "pid", "socketPath": reportedSocket,
            ])
            return String(decoding: data, as: UTF8.self)
        })
        let note = await daemon.restartIfRunning(homeURL: home, environment: [:])
        let expected = expectedRestart ? ["version", "restart"] : ["version"]
        let passed = calls == expected && note == nil
        print("calls=\\(calls) expected=\\(expected) passed=\\(passed)")
        exit(passed ? 0 : 1)
    }
}
'''

def harness(source):
    # Keep restartIfRunning and its decoding structs verbatim. Only unavailable
    # app dependencies and the unused real subprocess runner are replaced.
    production = source[source.index('@MainActor'):source.index('    private static func runCommand')]
    runner = '''    private static func runCommand(_ command: String, environment: [String: String]) async throws -> String {
        throw ProofError.unexpectedRealCommand
    }
}
'''
    return stubs + production + runner + main

results = []
start = time.monotonic()
for revision, source in [('baseline', baseline), ('candidate', candidate)]:
    swift_file = out / f'{revision}-socket-proof.swift'
    binary = out / f'{revision}-socket-proof'
    swift_file.write_text(harness(source))
    subprocess.run(['swiftc', '-parse-as-library', str(swift_file), '-o', str(binary)], check=True)
    for filename in ['daemon.pid', 'app-server.pid']:
        for shape in ['plain', 'symlink', 'resolved-symlink', 'other-home-symlink']:
            with tempfile.TemporaryDirectory(prefix='cb-socket-', dir='/private/tmp') as root:
                root = Path(root)
                home = root / 'home'
                (home / 'app-server-daemon').mkdir(parents=True)
                (home / 'app-server-daemon' / filename).write_text('{"pid":123}')
                alias = home / 'app-server-control/app-server-control.sock'
                alias.parent.mkdir()
                physical = alias if shape == 'plain' else root / 'actual.sock'
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(str(physical))
                    server.listen(1)
                    if shape != 'plain': alias.symlink_to(physical)
                    reported = physical if shape == 'resolved-symlink' else alias
                    second = None
                    try:
                        if shape == 'other-home-symlink':
                            other_alias = root / 'other/app-server-control/app-server-control.sock'
                            other_alias.parent.mkdir(parents=True)
                            other_physical = root / 'other.sock'
                            second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            second.bind(str(other_physical))
                            second.listen(1)
                            other_alias.symlink_to(other_physical)
                            reported = other_alias
                        command = [str(binary), str(home), str(reported),
                                   'reject' if shape == 'other-home-symlink' else 'restart']
                        run = subprocess.run(command, capture_output=True, text=True)
                        result = {'revision': revision, 'pidRecord': filename, 'shape': shape,
                                  'exitCode': run.returncode, 'result': run.stdout.strip()}
                        results.append(result)
                        print(json.dumps(result), flush=True)
                    finally:
                        if second: second.close()

baseline_failures = sum(r['exitCode'] != 0 for r in results if r['revision'] == 'baseline')
candidate_failures = sum(r['exitCode'] != 0 for r in results if r['revision'] == 'candidate')
summary = {'baselineFailures': baseline_failures, 'candidateFailures': candidate_failures,
           'scenariosPerRevision': 8, 'durationSeconds': round(time.monotonic() - start, 1)}
(out / 'socket-fixture-proof.json').write_text(json.dumps({'summary': summary, 'results': results}, indent=2) + '\n')
print(json.dumps(summary), flush=True)
assert baseline_failures == 4, summary
assert candidate_failures == 0, summary
