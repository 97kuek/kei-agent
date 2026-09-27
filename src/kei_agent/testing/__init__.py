"""モジュールを作る人のためのテストの道具（docs/extensibility.md の「作る人への支え」）。

- ModuleKit … モジュール1つを、本番と同じ読み方の設定と、偽物の Slack・AI・Notion・担当と一緒に本体の中で動かす。
  依頼者として頼む（message）、スラッシュコマンド（slash）、ボタン（action）、定期処理（schedule）、担当の仕事（skill）
- 偽物 … FakeSlack（投稿を残す）、FakeAI（AI の代わり。answer で答えを並べる）、FakeNotion・FakeHub（研究ホームと
  共通ホーム）、FakeNotionAPI（ゲートウェイの後ろの Notion）、FakePueue（ジョブ）、FakeAgent（担当プロセスの代わり）、
  LocalAgent（agent.py を同じプロセスの中で動かす）
- pytest の plugin（kei_agent.testing.plugin）… 本物の秘密情報・状態・launchd に触れないようにし、module_kit を出す

ここ（__init__）は pytest を読み込まない（plugin だけが使う）。
"""

from kei_agent.testing.agents import FakeAgent, LocalAgent
from kei_agent.testing.fakes import FakeAI, FakeHub, FakeNotion, FakeNotionAPI, FakePueue, FakeSlack
from kei_agent.testing.kit import BOT, OWNER, ModuleKit, settle

__all__ = ["BOT", "OWNER", "FakeAI", "FakeAgent", "FakeHub", "FakeNotion", "FakeNotionAPI", "FakePueue", "FakeSlack",
           "LocalAgent", "ModuleKit", "settle"]
