"""A running worker's events as short, safe, operator-facing lines."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import runpy
import unittest

from forge.factory.activity import command_line, tool_line, merge, events, public, mode, LIMIT

ROOT = Path(__file__).resolve().parents[1]


def tool(name, **data):
    return json.dumps({'type': 'forge.activity', 'id': 'toolu_' + name + str(len(data)), 'tool': name, 'input': data, 'at': 1005})


class ActivityLinesTest(unittest.TestCase):
    def test_commands_read_as_what_the_agent_is_doing(self):
        cases = {
            "gh issue view 7 --comments": "Reading issue #7",
            "gh pr view 28 --json files": "Opening PR #28",
            "gh pr diff 28": "Opening PR #28",
            "/bin/bash -lc 'cd /work/repository && rg -n coach src'": "Searching the repository for `coach`",
            "cd /work/repository && npm test": "Running npm test",
            "npm run lint -- --fix": "Running npm run lint",
            "git log --oneline -20": "Inspecting git history",
            "sed -n 1,80p /work/repository/src/app.ts": "Reading src/app.ts",
            "find . -name '*.spec.ts'": "Looking for files matching `*.spec.ts`",
            "GH_TOKEN=abc123 gh pr view 28": "Opening PR #28",
            "pytest -q tests/test_x.py": "Running pytest tests/test_x.py",
        }
        for command, line in cases.items():
            with self.subTest(command=command):
                self.assertEqual(command_line(command), line)

    def test_tools_read_as_what_the_agent_is_doing(self):
        self.assertEqual(tool_line('Read', {'file_path': '/work/repository/ui/app/coach.ts'}), 'Reading ui/app/coach.ts')
        self.assertEqual(tool_line('Read', {'file_path': '/work/home/notes.json'}), 'Reading the saved evidence')
        self.assertEqual(tool_line('Grep', {'pattern': 'coach'}), 'Searching the repository for `coach`')
        self.assertEqual(tool_line('StructuredOutput', {'summary': 'the whole answer'}), 'Writing the answer')
        self.assertEqual(tool_line('text', {}), 'Thinking it through')

    def test_no_secrets_raw_output_or_long_lines(self):
        github_token = 'ghp_' + 'X' * 36
        provider_key = 'sk-' + 'X' * 26
        risky = [
            f"curl -H 'Authorization: Bearer {github_token}' https://api.github.com",
            f"echo ok && grep -r {provider_key} .",
            "rg " + "x" * 500,
            f"export OPENAI_API_KEY={provider_key} && codex",
            "mysql --password=hunter2 -e 'select 1'",
        ]
        for command in risky:
            with self.subTest(command=command):
                line = command_line(command)
                self.assertLessEqual(len(line), LIMIT)
                for secret in ('ghp_', 'sk-', 'hunter2', 'Bearer', 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'):
                    self.assertNotIn(secret, line)
        self.assertLessEqual(len(tool_line('Read', {'file_path': '/work/repository/' + 'a/' * 200 + 'b.ts'})), LIMIT)
        self.assertLessEqual(len(tool_line('Grep', {'pattern': 'token'})), LIMIT)
        self.assertNotIn('token', tool_line('Grep', {'pattern': 'token'}))
        logs = '\n'.join([
            json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'FILE CONTENTS ghp_secret'}]}}),
            json.dumps({'type': 'item.completed', 'item': {'id': 'i', 'type': 'command_execution', 'aggregated_output': 'OUTPUT'}}),
        ])
        self.assertEqual(events(logs), [])

    def test_merge_dedupes_a_sliding_tail_and_keeps_eight(self):
        logs = '\n'.join(tool('Grep', pattern='p%d' % i) for i in range(3))
        activity, seen = merge([], [], logs, 1010, 1000)
        again, seen = merge(activity, seen, logs, 1020, 1000)
        self.assertEqual(activity, again)
        self.assertEqual(activity[0]['at'], 1005)
        many = '\n'.join(json.dumps({'type': 'forge.activity', 'id': str(i), 'tool': 'Grep', 'input': {'pattern': 'p%d' % i}}) for i in range(20))
        activity, _ = merge(activity, seen, many, 1030, 1000)
        self.assertEqual(len(public(activity)), 8)
        self.assertEqual(set(public(activity)[0]), {'at', 'text'})

    def test_coarse_phases_without_structured_events(self):
        alive = json.dumps({'runner': 'alive', 'attempt': 'x', 'at': 1001})
        activity, seen = merge([], [], alive, 1010, 1000)
        self.assertEqual([a['text'] for a in activity], ['Reading the saved evidence'])
        activity, seen = merge(activity, seen, alive, 1050, 1000)
        self.assertEqual([a['text'] for a in activity], ['Reading the saved evidence', 'Inspecting the repository'])
        activity, seen = merge(activity, seen, json.dumps({'type': 'claude.result', 'summary': 'x'}), 1060, 1000)
        self.assertEqual(activity[-1]['text'], 'Writing the answer')

    def test_mode_says_whether_the_agent_answers_from_evidence_or_checks_the_code(self):
        lines = lambda *texts: [{'at': 1, 'text': t} for t in texts]
        self.assertEqual(mode([]), 'evidence')
        self.assertEqual(mode(lines('Getting started', 'Reading the saved evidence', 'Reading issue #7',
                                    'Opening PR #3', 'Querying GitHub', 'Thinking it through', 'Writing the answer')), 'evidence')
        for code in ('Searching the repository for `x`', 'Reading src/a.ts', 'Looking around the repository',
                     'Inspecting the repository', 'Running npm test'):
            self.assertEqual(mode(lines('Getting started', code)), 'code', code)

    def test_claude_stream_events_map_too(self):
        logs = json.dumps({'type': 'assistant', 'message': {'id': 'm1', 'content': [
            {'type': 'thinking', 'thinking': 'private'},
            {'type': 'text', 'text': 'Let me look'},
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'gh issue view 7'}}]}})
        self.assertEqual([line for _, _, line in events(logs)], ['Thinking it through', 'Reading issue #7'])


class AnnouncerTest(unittest.TestCase):
    """The image-owned adapter half: tool calls only, never results or text."""

    def test_announces_tool_calls_across_block_boundaries(self):
        module = runpy.run_path(str(ROOT / 'forge/harnesses/activity.py'))
        printed = []
        announce = module['Announcer'](write=lambda line, flush=False: printed.append(json.loads(line)))
        use = json.dumps({'type': 'assistant', 'message': {'id': 'm1', 'content': [
            {'type': 'text', 'text': 'private narration'},
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'gh issue view 7', 'env': 'SECRET', 'content': 'x' * 900}}]}}).encode()
        result = json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'FILE CONTENTS'}]}}).encode()
        stream = use + b'\n' + result + b'\n'
        announce.feed(stream[:30]); announce.feed(stream[30:])
        self.assertEqual([(e['tool'], e['input']) for e in printed], [('text', {}), ('Bash', {'command': 'gh issue view 7'})])
        self.assertNotIn('private narration', json.dumps(printed))
        self.assertNotIn('FILE CONTENTS', json.dumps(printed))

    def test_adapters_announce_while_collecting(self):
        for name in ('claude', 'grok'):
            source = (ROOT / 'forge/harnesses' / name / 'adapter.py').read_text()
            self.assertIn('announce.feed(block)', source)

    def test_announcer_never_breaks_a_run(self):
        module = runpy.run_path(str(ROOT / 'forge/harnesses/activity.py'))
        def broken(*args, **kwargs): raise OSError('closed')
        announce = module['Announcer'](write=broken)
        announce.feed(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't', 'name': 'Read', 'input': {}}]}}).encode() + b'\n')
        with redirect_stdout(io.StringIO()):
            announce.feed(b'not json\n')


if __name__ == '__main__': unittest.main()
