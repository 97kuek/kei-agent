"""文書の図（全体図 overview.svg、プロセスと通信の図 architecture.svg、Dot を頭にした構想図 dots-plan.svg）を作る。

アイコンは icons/ の PNG を SVG に埋め込む（GitHub でそのまま見える）。図を直すときは、ここを書き換えて
`uv run python docs/images/diagrams.py` を動かす。文字が枠からはみ出しそうなら、そこを知らせる。
"""

import base64
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONT = "'Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'Noto Sans JP', 'Yu Gothic', sans-serif"
# 塗りと線
HOST = ("#eef4ff", "#9db4e0")
MODULE = ("#f4f0ff", "#b6a8e2")
AGENT = ("#eefaf2", "#95c9a9")
AI = ("#fff5eb", "#e6b88a")
OUTSIDE = ("#f6f7f9", "#c3c9d4")
FRAME = ("#fbfcfe", "#d3d9e4")
TEXT, SUB, LINE = "#1f2937", "#566070", "#8a94a6"


def text_width(text: str, size: float) -> float:
    """文字の幅の目安（全角は1字、半角は0.6字）。"""
    return sum(size if ord(c) > 0x2E80 else size * 0.6 for c in text)


class Drawing:
    def __init__(self, name: str, width: int, height: int):
        self.name, self.width, self.height = name, width, height
        self.parts: list[str] = []

    def _check(self, text: str, size: float, room: float) -> None:
        if text_width(text, size) > room:
            print(f"⚠️ {self.name}: はみ出しそう「{text}」（{text_width(text, size):.0f} > {room:.0f}）")

    def text(self, x, y, text, size=13, color=TEXT, anchor="start", weight="normal", room=None):
        if room is not None:
            self._check(text, size, room)
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}" '
                          f'font-weight="{weight}">{escape(text)}</text>')

    def icon(self, name, x, y, size):
        data = base64.b64encode((HERE / "icons" / f"{name}.png").read_bytes()).decode()
        self.parts.append(f'<image x="{x}" y="{y}" width="{size}" height="{size}" href="data:image/png;base64,{data}"/>')

    def speaker(self, x, y, size):
        """スピーカーの絵（アイコンの線画と同じ太さで描く）。"""
        s = size / 24
        self.parts.append(
            f'<g transform="translate({x},{y}) scale({s})" fill="none" stroke="#111" stroke-width="1.8" '
            'stroke-linejoin="round" stroke-linecap="round"><path d="M3 9h4l5-4v14l-5-4H3z"/>'
            '<path d="M16 9a4 4 0 0 1 0 6"/><path d="M18.5 6.5a7.5 7.5 0 0 1 0 11"/></g>')

    def box(self, x, y, w, h, colors, dash=False, radius=12):
        fill, stroke = colors
        dashed = ' stroke-dasharray="6 4"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" '
                          f'stroke="{stroke}" stroke-width="1.5"{dashed}/>')

    def _icons(self, icons, x, y, size):
        for i, name in enumerate(icons):
            if name == "speaker":
                self.speaker(x + i * (size + 4), y, size)
            else:
                self.icon(name, x + i * (size + 4), y, size)
        return len(icons) * (size + 4)

    def frame(self, x, y, w, h, title, colors=FRAME, icons=(), dash=False):
        """まとまりの枠（左上に見出し）。"""
        self.box(x, y, w, h, colors, dash=dash, radius=16)
        used = self._icons(icons, x + 14, y + 10, 24)
        self.text(x + 14 + used, y + 28, title, size=14, weight="bold", room=w - 28 - used)

    def card(self, x, y, w, h, title, lines=(), colors=AGENT, icons=(), links=(), dash=False, size=30):
        """札（左にアイコン、右に見出しと説明。links は右下に小さく並べるつながる先のアイコン）。"""
        self.box(x, y, w, h, colors, dash=dash)
        used = self._icons(icons, x + 12, y + 12, size)
        left = x + 12 + used + (4 if icons else 0)
        room = x + w - 10 - left
        self.text(left, y + 30, title, size=15, weight="bold", room=room)
        for i, line in enumerate(lines):
            self.text(left, y + 52 + i * 19, line, size=12, color=SUB, room=room)
        if links:
            self._icons(links, x + w - 12 - len(links) * 26 + 4, y + h - 32, 22)

    def arrow(self, points, label="", both=False, dash=False, label_at=None):
        path = " ".join(f"{px},{py}" for px, py in points)
        start = ' marker-start="url(#head)"' if both else ""
        dashed = ' stroke-dasharray="5 4"' if dash else ""
        self.parts.append(f'<polyline points="{path}" fill="none" stroke="{LINE}" stroke-width="1.8" '
                          f'marker-end="url(#head)"{start}{dashed}/>')
        if label:
            (x1, y1), (x2, y2) = points[len(points) // 2 - 1], points[len(points) // 2]
            lx, ly = label_at or ((x1 + x2) / 2, (y1 + y2) / 2)
            width = text_width(label, 12) + 10
            self.parts.append(f'<rect x="{lx - width / 2}" y="{ly - 11}" width="{width}" height="18" rx="4" '
                              'fill="#ffffff" fill-opacity="0.95"/>')
            self.text(lx, ly + 3, label, size=12, color=SUB, anchor="middle")

    def svg(self) -> str:
        return "\n".join([
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}" font-family="{escape(FONT)}">',
            '<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{LINE}"/></marker></defs>',
            f'<rect width="{self.width}" height="{self.height}" fill="#ffffff"/>',
            *self.parts, "</svg>", ""])


def overview() -> Drawing:
    """全体図: 依頼者が Slack で Dot に頼み、Dot が自分で答えるか、Mac の Kei Agent の手の口で担当に作業させる。"""
    d = Drawing("overview", 1180, 600)
    d.card(30, 60, 190, 64, "依頼者", colors=OUTSIDE, icons=("user",))
    d.arrow([(125, 124), (125, 196)], "Slack で頼む")
    d.card(30, 196, 190, 96, "Slack", ("Dot に話しかける", "@ueki.keitaro-kei"), colors=OUTSIDE, icons=("slack",))
    d.arrow([(220, 244), (262, 244)], both=True)

    d.frame(262, 20, 300, 560, "OpenAI（クラウド）", colors=AI, icons=("chatgpt",))
    d.card(280, 64, 264, 136, "Dot（頭）", ("Slack の受け答え", "どの手を使うかを決める", "Mac を閉じていても答える"),
           colors=("#ffffff", AI[1]), icons=("chatgpt",), size=26)
    d.card(280, 214, 264, 136, "Dot の予定", ("朝の一覧と Daily・振り返り", "締切・読みもの・論文の新着", "夜の Task・Kei Agent の知らせ"),
           colors=("#ffffff", AI[1]), icons=("morning",), size=26)
    d.card(280, 364, 264, 120, "Dot の連携", ("Notion・Outlook の予定を", "直接読み書きする"),
           colors=("#ffffff", AI[1]), icons=("notion",), links=("outlook",), size=26)
    d.text(282, 520, "頼みごとに作業場が要るときだけ、", size=12, color=SUB)
    d.text(282, 540, "Kei Agent の手の口を呼ぶ", size=12, color=SUB)

    d.arrow([(562, 132), (600, 132)], "MCP", label_at=(581, 114))
    d.frame(600, 20, 560, 560, "Kei Agent（自分の Mac で常駐。Slack にはいない）", icons=("mac",))
    d.card(620, 64, 520, 96, "本体（手）", ("手の口（MCP）: 作業場で AI を動かす・材料・知らせ・時間の記録",
                                            "取り込み（Moodle・Toggl）・ジョブ・毎晩の保守とバックアップ"),
           colors=HOST, icons=("server",), size=26)
    d.text(622, 190, "担当（それぞれのプロセスで動く）", size=13, color=SUB, weight="bold")
    for i, (title, line, icons) in enumerate((
            ("研究", "テーマごとに作業", ("research",)),
            ("大学", "締切・授業・要項", ("calendar",)),
            ("仕事", "会社のメールと資料", ("work",)),
            ("知識", "記事と論文", ("document",)),
            ("声", "知らせを喋る", ("speaker",)),
            ("Notion の口", "Notion の出入口", ("notion",)))):
        d.card(620 + (i % 3) * 176, 200 + (i // 3) * 92, 164, 80, title, (line,), icons=icons, size=26)
    d.frame(620, 400, 260, 160, "AI（担当ごとに選ぶ）", colors=AI)
    d.card(632, 440, 116, 100, "Claude", ("Code",), colors=("#ffffff", AI[1]), icons=("claude",), size=24)
    d.card(754, 440, 116, 100, "Codex", ("CLI",), colors=("#ffffff", AI[1]), icons=("chatgpt",), size=24)
    d.frame(896, 400, 244, 160, "つながる先", colors=OUTSIDE)
    for i, (title, icons) in enumerate((
            ("Moodle・Box", ("moodle", "box")), ("Microsoft 365", ("outlook", "teams", "sharepoint")),
            ("Toggl・GitHub", ("toggl", "github")))):
        d.card(908, 436 + i * 40, 220, 34, title, colors=("#ffffff", OUTSIDE[1]), icons=icons, size=20)
    return d


def architecture() -> Drawing:
    """プロセスと通信の図: Dot からトンネルで手の口へ。7つの常駐のプロセス、A2A、Notion ゲートウェイ、AI の CLI。"""
    d = Drawing("architecture", 1240, 680)
    d.card(30, 40, 180, 96, "Dot", ("OpenAI のクラウド", "Slack の受け口"), colors=AI, icons=("chatgpt",))
    d.arrow([(120, 136), (120, 196)], "MCP", label_at=(120, 166))
    d.card(30, 196, 180, 112, "トンネル", ("Secure MCP Tunnel", "Mac から外へ", "出ていくだけ"), colors=OUTSIDE,
           icons=("server",), size=26)
    d.frame(240, 16, 980, 648, "Mac（launchd が本体・担当・ゲートウェイ・トンネルを常駐させ、落ちたら起こし直す）", icons=("mac",))
    d.arrow([(210, 252), (258, 252), (258, 152), (282, 152)])
    d.card(282, 56, 630, 176, "本体 :8786 ・手の口 :8785", (
        "手の口（MCP）: workspaces・run・status・create_workspace・timer・voice",
        "put_file・read_file・notices・agenda・reading・recent・jobs（と post）",
        "知らせの置き場（Outbox）: Slack に出すつもりの投稿をためて、Dot が notices で読む",
        "定期処理（取り込みと保守）・制限の表・状態（SQLite）・声からの問い合わせ",
        "Slack のトークンがあれば Slack の受け口にもなる（今は使わない）"),
        colors=HOST, icons=("server",), links=("database", "toggl"))
    d.card(942, 56, 256, 136, "Notion ゲートウェイ :8791", (
        "Notion に届く唯一の口", "使う側ごとの合言葉で", "届くホームを絞る"),
        colors=("#ffffff", "#8b95a7"), icons=("notion",), dash=True, size=26)
    d.arrow([(912, 124), (942, 124)], dash=True)

    d.parts.append(f'<line x1="290" y1="278" x2="1170" y2="278" stroke="{LINE}" stroke-width="3"/>')
    d.text(296, 268, "A2A（127.0.0.1、共有の合言葉）。担当を呼べるのは本体だけ", size=12, color=SUB)
    d.arrow([(860, 232), (860, 276)])
    agents = (
        ("大学 :8787", ("課題の取り込み", "授業・要項・過去問"), ("calendar",), ("moodle", "box", "notion")),
        ("研究 :8788", ("テーマの作業場で作業", "長い処理はジョブ"), ("research",), ("notion",)),
        ("仕事 :8789", ("会社のメールと資料を", "読むだけで答える"), ("work",), ("outlook", "teams", "sharepoint")),
        ("知識 :8792", ("記事と論文を", "選んで要約する"), ("document",), ()),
        ("声 :8790", ("知らせを喋る", "マイクで会話（任意）"), ("speaker",), ()),
    )
    for i, (title, lines, icons, links) in enumerate(agents):
        x = 262 + i * 188
        d.arrow([(x + 88, 280), (x + 88, 318)])
        d.card(x, 318, 176, 150, title, lines, icons=icons, links=links, size=26)
    for i in range(4):
        x = 262 + i * 188 + 88
        d.arrow([(x, 468), (x, 510)])
    d.frame(262, 510, 740, 132, "AI の CLI（担当ごとに agents.csv で Claude Code か Codex を選ぶ）", colors=AI,
            icons=("claude", "chatgpt"))
    d.text(282, 566, "使える道具と届く範囲は、モジュールの宣言から作る制限の表で決まる", size=12, color=SUB)
    d.text(282, 586, "sandbox の中で動かし、作業場の外には書かない", size=12, color=SUB)
    d.text(282, 606, "Dot が選べるのは作業場・頼みごと・重さ・表で許した AI だけ", size=12, color=SUB)
    d.text(1024, 532, "点線は Notion への道。", size=12, color=SUB)
    d.text(1024, 552, "本体・研究・大学のどれも、", size=12, color=SUB)
    d.text(1024, 572, "ゲートウェイを通して", size=12, color=SUB)
    d.text(1024, 592, "Notion に届く", size=12, color=SUB)
    return d


def dots_plan() -> Drawing:
    """構想図（GitHub issue #17）: Dot が頭、Kei Agent が手。いつも動く手は研究室のサーバー、仕事は MacBook Air。"""
    d = Drawing("dots-plan", 1240, 760)
    d.card(30, 40, 200, 96, "依頼者", ("Slack・ChatGPT から", "Dot に頼む"), colors=OUTSIDE, icons=("user",))
    d.arrow([(130, 136), (130, 176)])
    d.card(30, 176, 200, 108, "Stack-chan", ("ふだんの会話は", "GPT-Live-1 で速く", "頼みごとは Dot へ"),
           colors=OUTSIDE, icons=("speaker",), size=26)
    d.arrow([(230, 88), (262, 88)])
    d.arrow([(230, 230), (262, 230)])

    d.frame(262, 20, 958, 280, "OpenAI（クラウド。いつも動く役は Dot が受け持つ）", colors=AI, icons=("chatgpt",))
    d.card(282, 64, 300, 216, "Dot（頭）", (
        "Slack の受け答え（済み）", "予定: Daily・振り返り・締切・", "読みもの・論文・夜の Task（済み）",
        "途中の確認は Dot が答える", "（作法の範囲だけ。線は越えられない）", "Stack-chan の道具で喋らせる"),
        colors=("#ffffff", AI[1]), icons=("chatgpt",), size=28)
    d.card(602, 64, 290, 100, "Notion・Outlook", ("AI が書くものは Dot が直接（済み）",
                                                  "会社の予定も Dot が読む（済み）"),
           colors=("#ffffff", AI[1]), icons=("notion",), links=("outlook",), size=26)
    d.card(602, 180, 290, 100, "Codex クラウド", ("自己改善は GitHub の PR で", "（Mac の自己改善はいずれ消す）"),
           colors=("#ffffff", AI[1]), icons=("code",), links=("github",), size=26)
    d.card(912, 64, 288, 216, "GPT-Live-1", ("Stack-chan の声の会話", "", "Dot と同じ ChatGPT Pro の", "中で動かす"),
           colors=("#ffffff", AI[1]), icons=("speaker",), size=26)

    d.arrow([(640, 300), (640, 360)], "Secure MCP Tunnel（機械から外へ出るだけ）", label_at=(640, 330))

    d.frame(262, 360, 700, 300, "研究室のサーバー（いつも動く。③ でここへ移す。今は MacBook Air）", icons=("server",))
    d.card(282, 404, 440, 96, "手の口（MCP）とトンネル", ("作業場で AI を動かす・知らせ・時間の記録",
                                                         "材料（予定・読みもの・動き・ジョブ）"),
           colors=HOST, icons=("server",), size=26)
    d.card(282, 512, 214, 128, "取り込みと保守", ("Moodle・Toggl", "（鍵は手元だけ）", "保守とバックアップ"),
           colors=MODULE, icons=("database",), links=("moodle", "toggl"), size=24)
    d.card(508, 512, 214, 128, "Notion の口", ("機械的な取り込み", "だけが書く"),
           colors=("#ffffff", "#8b95a7"), icons=("notion",), dash=True, size=24)
    d.card(742, 404, 200, 236, "研究", ("テーマの作業場で", "Claude Code・Codex", "長い処理はジョブ", "（個人のアカウント）"),
           icons=("research",), size=24)

    d.arrow([(962, 452), (1002, 452)], "A2A", label_at=(982, 434))
    d.frame(1002, 360, 218, 300, "MacBook Air", icons=("mac",))
    d.card(1016, 404, 190, 236, "仕事", ("会社のアカウントの", "Claude Code", "メール・Teams・資料は", "ここだけで読む",
                                      "Mac を開いている", "間だけ使える"),
           icons=("work",), links=("outlook", "teams", "sharepoint"), size=24)

    d.text(262, 702, "越えてはいけない線（秘密情報・鍵、会社と個人のアカウントを混ぜない、作業場の外に書かない、外へ送らない）は、"
                     "Kei Agent が", size=12, color=SUB)
    d.text(262, 722, "Claude にも Codex にも同じ固さで守らせる。頭（Dot）が選べるのは作業場・頼みごと・重さ・表で許した AI だけ",
           size=12, color=SUB)
    return d


def main() -> None:
    for drawing in (overview(), architecture(), dots_plan()):
        path = HERE / f"{drawing.name}.svg"
        path.write_text(drawing.svg(), encoding="utf-8")
        print(f"書いた: {path.relative_to(HERE.parent.parent)}")


if __name__ == "__main__":
    main()
