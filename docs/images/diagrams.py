"""文書の4図を、ローカルのアイコン・正式名・接続方式で作る。

`uv run python docs/images/diagrams.py` で SVG を作り直す。
アイコンを埋め込むので、GitHub でも単独で表示できる。
"""

import base64
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONT = "'Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'Noto Sans JP', 'Yu Gothic', sans-serif"
TEXT = "#233044"
LINE = "#61758b"
CLOUD = ("#fff8f0", "#e5c7a8")
MAC = ("#f1f6fc", "#b5c9df")
NEUTRAL = ("#fafbfd", "#d8e0e9")


class Drawing:
    def __init__(self, name: str, width: int, height: int, title: str, caption: str):
        self.name, self.width, self.height, self.title = name, width, height, title
        self.parts: list[str] = []
        self.text(20, 25, title, size=18, anchor="start", weight="bold")
        self.text(20, 47, caption, size=12, anchor="start", color=LINE)

    def text(self, x, y, text, size=14, color=TEXT, anchor="middle", weight="normal"):
        self.parts.append(
            f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{escape(text)}</text>'
        )

    def icon(self, name, x, y, size=44):
        """PNG と SVG は必ず icons/ から読み、外部参照を残さない。"""
        path = HERE / "icons" / f"{name}.svg"
        if not path.exists():
            path = HERE / "icons" / f"{name}.png"
        mime = "image/svg+xml" if path.suffix == ".svg" else "image/png"
        data = base64.b64encode(path.read_bytes()).decode()
        self.parts.append(
            f'<image x="{x}" y="{y}" width="{size}" height="{size}" '
            f'href="data:{mime};base64,{data}"/>'
        )

    def node(self, x, y, icon, label, caption="", size=44, label_size=14):
        self.icon(icon, x - size / 2, y - size / 2, size)
        labels = label if isinstance(label, tuple) else (label,)
        for i, line in enumerate(labels):
            self.text(x, y + size / 2 + 19 + i * 17, line, size=label_size, weight="bold")
        if caption:
            self.text(x, y + size / 2 + 19 + len(labels) * 17, caption, size=11, color=LINE)

    def frame(self, x, y, width, height, label="", colors=NEUTRAL):
        fill, stroke = colors
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="12" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>'
        )
        if label:
            self.text(x + 15, y + 25, label, size=14, anchor="start", weight="bold")

    def line(self, points):
        coords = " ".join(f"{x},{y}" for x, y in points)
        self.parts.append(
            f'<polyline points="{coords}" fill="none" stroke="{LINE}" '
            'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>'
        )

    def arrow(self, points, label, both=False, label_at=None):
        """すべての矢印に、接続方式または行われる操作を付ける。"""
        coords = " ".join(f"{x},{y}" for x, y in points)
        start = ' marker-start="url(#head)"' if both else ""
        self.parts.append(
            f'<polyline points="{coords}" fill="none" stroke="{LINE}" stroke-width="1.8" '
            f'stroke-linejoin="round" marker-end="url(#head)"{start}/>'
        )
        (x1, y1), (x2, y2) = points[0], points[-1]
        x, y = label_at or ((x1 + x2) / 2, (y1 + y2) / 2 - 9)
        width = sum(12 if ord(c) > 0x2E80 else 7 for c in label) + 10
        self.parts.append(
            f'<rect x="{x - width / 2}" y="{y - 12}" width="{width}" height="17" '
            'rx="3" fill="#ffffff"/>'
        )
        self.text(x, y, label, size=12, color=LINE, weight="bold")

    def svg(self):
        return "\n".join([
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}" font-family="{escape(FONT)}" role="img" '
            'aria-labelledby="title">',
            f'<title id="title">{escape(self.title)}</title>',
            '<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
            f'markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{LINE}"/>'
            '</marker></defs>',
            f'<rect width="{self.width}" height="{self.height}" fill="#ffffff"/>',
            *self.parts, '</svg>', '',
        ])


def overview() -> Drawing:
    d = Drawing("overview", 800, 500, "Kei Agent の全体像",
                "Kei Agent は、クラウドの Dot と Mac の実行サービスを合わせたアシスタント。")
    d.frame(193, 65, 589, 418, "Kei Agent")
    d.frame(210, 106, 252, 359, "クラウド", CLOUD)
    d.frame(551, 106, 214, 359, "ローカル", MAC)
    d.node(80, 108, "user", "あなた", size=40)
    d.node(80, 241, "slack", "Slack")
    d.node(80, 400, "phone", "通話", "依頼・結果は Slack へ")
    d.arrow([(80, 171), (80, 211)], "入力", label_at=(107, 193))
    d.arrow([(50, 108), (25, 108), (25, 400), (50, 400)], "音声", label_at=(25, 313))
    d.node(337, 241, "chatgpt", "Dot", "対話・大学・知識・Outlook", size=48)
    d.arrow([(111, 241), (303, 241)], "Slack 接続", both=True)
    d.arrow([(111, 400), (175, 400), (175, 276), (303, 276)], "音声 API", both=True,
            label_at=(229, 268))
    d.node(658, 241, "mac", "Mac", "担当を動かす", size=48)
    d.arrow([(371, 241), (624, 241)], "MCP", both=True)
    d.node(273, 395, "code", "プラグイン", "外部サービス")
    d.node(410, 395, "calendar", "定期処理", "Daily・振り返り")
    d.arrow([(320, 307), (273, 342), (273, 365)], "API", label_at=(283, 333))
    d.arrow([(356, 307), (410, 342), (410, 365)], "予定", label_at=(398, 333))
    d.frame(569, 331, 178, 113, "Mac の担当")
    d.text(658, 381, "AI: 研究・仕事", size=12, weight="bold")
    d.text(658, 405, "機械同期: Moodle", size=12, weight="bold")
    d.text(658, 427, "秘密情報と作業場は Mac", size=10, color=LINE)
    d.arrow([(658, 307), (658, 331)], "A2A", label_at=(694, 320))
    return d


def architecture() -> Drawing:
    d = Drawing("architecture", 800, 610, "Mac の実行構造",
                "Dot の依頼は MCP、担当への依頼は A2A。Notion はローカルの Gateway を通す。")
    d.frame(152, 66, 516, 525, "Mac", MAC)
    d.node(62, 147, "chatgpt", "Dot", size=48)
    d.node(233, 147, "server", ("Secure MCP", "Tunnel"), "OpenAI", label_size=13)
    d.node(413, 147, "server", "Kei Agent MCP", "本体・通知・状態", label_size=13)
    d.node(591, 147, "server", ("Notion", "Gateway"), "ローカル API / MCP", label_size=13)
    d.node(743, 147, "notion", "Notion")
    d.arrow([(95, 147), (202, 147)], "MCP", both=True)
    d.arrow([(264, 147), (382, 147)], "MCP", both=True)
    d.arrow([(444, 147), (560, 147)], "API", both=True)
    d.arrow([(622, 147), (712, 147)], "Notion API", both=True)
    d.line([(413, 212), (413, 252)])
    d.line([(224, 252), (614, 252)])
    agents = ((224, "research", "研究エージェント", "個人の AI"),
              (413, "work", "仕事エージェント", "会社の Claude"),
              (614, "moodle", "Moodle 同期", "AI を使わない"))
    for x, icon, label, caption in agents:
        d.arrow([(x, 252), (x, 303)], "A2A", label_at=(x + 23, 278))
        d.node(x, 334, icon, label, caption, label_size=12)
    d.line([(224, 407), (224, 425), (354, 425)])
    d.frame(176, 478, 237, 95, "研究", CLOUD)
    d.node(238, 520, "claude", "Claude Code", size=40, label_size=12)
    d.node(354, 520, "chatgpt", "Codex", size=40, label_size=12)
    d.arrow([(238, 425), (238, 494)], "CLI", label_at=(257, 455))
    d.arrow([(354, 425), (354, 494)], "CLI", label_at=(373, 455))
    d.frame(447, 478, 202, 95, "会社", CLOUD)
    d.node(550, 520, "claude", "Claude Code", size=40, label_size=12)
    d.arrow([(413, 407), (413, 455), (550, 455), (550, 494)], "CLI", label_at=(490, 447))
    d.text(62, 231, "大学・知識は", size=11, color=LINE)
    d.text(62, 248, "クラウドで直接", size=11, color=LINE)
    d.text(743, 262, "共通・研究・授業", size=11, color=LINE)
    d.text(743, 279, "の Notion ホーム", size=11, color=LINE)
    d.text(614, 426, "起動・復帰 + 30分 ICS", size=10, color=LINE)
    d.text(614, 443, "提出・受験は10分 API", size=10, color=LINE)
    d.text(743, 364, "Microsoft 365", size=12, weight="bold")
    d.node(743, 422, "teams", ("Microsoft", "Teams"), size=40, label_size=12)
    d.node(743, 539, "sharepoint", "SharePoint", "読むだけ", size=40, label_size=12)
    d.line([(580, 520), (686, 520)])
    d.arrow([(686, 520), (686, 422), (716, 422)], "MCP", label_at=(686, 459))
    d.arrow([(686, 520), (686, 539), (716, 539)], "MCP", label_at=(686, 559))
    return d


def dots_plan() -> Drawing:
    d = Drawing("dots-plan", 800, 600, "Dot と Mac の役割",
                "会話・プラグイン・予定はクラウド。秘密情報が必要な取り込みは Mac。")
    d.frame(168, 65, 416, 518, "クラウド（Dot）", CLOUD)
    d.frame(184, 104, 384, 214, "接続するプラグイン")
    plugins = ((235, 158, "notion", "Notion"), (327, 158, "box", "Box"),
               (419, 158, "outlook", ("Microsoft", "Outlook")),
               (511, 158, "google-calendar", ("Google", "Calendar")),
               (247, 260, "gmail", "Gmail"), (376, 260, "google-drive", "Google Drive"),
               (505, 260, "github", "GitHub"))
    for x, y, icon, label in plugins:
        d.node(x, y, icon, label, size=40, label_size=12)
    d.node(376, 392, "chatgpt", "Dot", "大学・知識の質問も直接担当", size=48)
    d.arrow([(376, 362), (376, 318)], "プラグイン API", label_at=(434, 345))
    d.node(72, 392, "slack", "Slack")
    d.node(72, 529, "phone", "通話", "依頼・結果は Slack へ")
    d.arrow([(102, 392), (342, 392)], "Slack 接続", both=True)
    d.arrow([(102, 529), (148, 529), (148, 428), (342, 428)], "音声 API", both=True,
            label_at=(235, 420))
    d.node(456, 521, "calendar", "定期処理", "Daily・振り返り・締切", size=40)
    d.arrow([(411, 461), (456, 493)], "予定", label_at=(445, 473))
    d.frame(610, 316, 174, 267, "ローカル（Mac）", MAC)
    d.node(697, 392, "mac", "Mac", "MCP・秘密情報", size=48)
    d.arrow([(410, 392), (662, 392)], "MCP", both=True)
    d.node(656, 521, "moodle", "Moodle", size=40, label_size=12)
    d.node(742, 521, "toggl-track", "Toggl Track", size=40, label_size=12)
    d.arrow([(675, 461), (656, 491)], "ICS / API", label_at=(650, 480))
    d.arrow([(717, 461), (742, 491)], "API", label_at=(752, 480))
    return d


def schedule() -> Drawing:
    d = Drawing("schedule", 800, 705, "1日の定期処理",
                "Dot が知らせを届け、Mac が取り込みと実行を受け持つ（時刻は日本時間）。")
    d.frame(113, 67, 341, 570, "クラウド", CLOUD)
    d.frame(472, 67, 308, 570, "ローカル", MAC)
    d.node(284, 114, "chatgpt", "Dot", size=40)
    d.node(626, 114, "mac", "Mac", size=40)
    rows = ((203, "07:00"), (276, "08:00"), (349, "18:00"),
            (422, "21:00"), (495, "22:00"), (568, "00:00"))
    for y, time in rows:
        d.text(62, y + 5, time, size=14, weight="bold")
        d.line([(126, y + 49), (440, y + 49)])
        d.line([(486, y + 49), (766, y + 49)])
    d.arrow([(25, 191), (25, 597)], "時刻が進む", label_at=(62, 632))
    d.node(160, 203, "document", "読みもの", size=40, label_size=12)
    d.text(308, 200, "#4-knowledge", size=13, weight="bold")
    d.text(308, 218, "1記事につき1親投稿", size=12, color=LINE)
    d.text(308, 236, "先行研究の新着は #0-overview", size=11, color=LINE)
    d.node(160, 276, "morning", "Daily", size=40, label_size=12)
    d.text(308, 273, "予定・Daily・締切", size=13, weight="bold")
    d.text(308, 294, "#0-overview", size=12, color=LINE)
    d.text(626, 273, "Moodle は時刻によらず同期", size=12, weight="bold")
    d.text(626, 294, "起動・復帰 / 30分 / 10分", size=11, color=LINE)
    d.node(160, 349, "calendar", "締切通知", size=40, label_size=12)
    d.text(308, 346, "近い締切を知らせる", size=13, weight="bold")
    d.text(308, 367, "#0-overview", size=12, color=LINE)
    d.node(160, 422, "document", "振り返り", size=40, label_size=12)
    d.text(308, 419, "成果・未完了・次の締切", size=12, weight="bold")
    d.text(308, 440, "#0-overview", size=12, color=LINE)
    d.node(525, 495, "toggl-track", "Toggl Track", size=40, label_size=12)
    d.text(662, 492, "時間記録を取り込む", size=13, weight="bold")
    d.text(662, 513, "API / 保守・バックアップ", size=11, color=LINE)
    d.node(160, 568, "document", "夜の Task", size=40, label_size=12)
    d.text(302, 565, "今夜の作業を依頼", size=13, weight="bold")
    d.text(302, 586, "結果は元のスレッドへ", size=12, color=LINE)
    d.arrow([(417, 568), (493, 568)], "MCP")
    d.node(525, 568, "server", "Kei Agent MCP", size=40, label_size=11)
    d.text(667, 565, "担当が CLI を実行", size=13, weight="bold")
    d.text(667, 586, "A2A → Claude Code / Codex", size=10, color=LINE)
    d.text(113, 658, "Dot: 毎時 notices → 元のチャンネル / 07:40 Outlook・Google Calendar の予定同期（API）",
           size=11, anchor="start", color=LINE)
    d.text(113, 678, "Mac: 起動・復帰と30分ごとに Moodle ICS、API 設定時は10分ごとに提出・受験終了を同期。",
           size=11, anchor="start", color=LINE)
    d.text(113, 698, "Mac の処理は、Mac が開いている間に動く。", size=11, anchor="start", color=LINE)
    return d


def main() -> None:
    for drawing in (overview(), architecture(), dots_plan(), schedule()):
        path = HERE / f"{drawing.name}.svg"
        path.write_text(drawing.svg(), encoding="utf-8")
        print(f"書いた: {path.relative_to(HERE.parent.parent)}")


if __name__ == "__main__":
    main()
