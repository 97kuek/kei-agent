// マイクから聞き続けて、確定した文字起こしを1行ずつ JSON で出す。
//
// Python からは呼べない API（macOS 26 の SpeechAnalyzer / SpeechTranscriber）だけを、ここで持つ。
// 呼びかけの判定も、そのあとの段取りも Python 側（`ears.py` と `wake.py`）。
// 測った値: 5.5 秒の音声が 0.11 秒で文字になる（docs/voice.md の9節）。
//
// 出す形（1行1件）:
//   {"kind":"ready"}
//   {"kind":"final","text":"けい今日の予定は"}
//   {"kind":"error","text":"…"}

import AVFoundation
import Foundation
import Speech

func emit(_ kind: String, _ text: String = "") {
    let payload: [String: String] = text.isEmpty ? ["kind": kind] : ["kind": kind, "text": text]
    guard let data = try? JSONSerialization.data(withJSONObject: payload),
          let line = String(data: data, encoding: .utf8) else { return }
    print(line)
    fflush(stdout)
}

let locale = Locale(identifier: "ja-JP")
let supported = await SpeechTranscriber.supportedLocales
guard supported.contains(where: { $0.identifier(.bcp47) == "ja-JP" }) else {
    emit("error", "日本語の文字起こしに対応していません")
    exit(1)
}

// 逐次（progressive）にして、喋り終わる前から結果を受け取る
let transcriber = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
let analyzer = SpeechAnalyzer(modules: [transcriber])

guard let target = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
    emit("error", "使える音の形が見つかりません")
    exit(1)
}

let engine = AVAudioEngine()
let input = engine.inputNode
let format = input.outputFormat(forBus: 0)
guard let converter = AVAudioConverter(from: format, to: target) else {
    emit("error", "マイクの音を変換できません")
    exit(1)
}

input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
    let ratio = target.sampleRate / format.sampleRate
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
    guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
    var error: NSError?
    var fed = false
    converter.convert(to: out, error: &error) { _, status in
        if fed { status.pointee = .noDataNow; return nil }
        fed = true
        status.pointee = .haveData
        return buffer
    }
    if error == nil, out.frameLength > 0 {
        continuation.yield(AnalyzerInput(buffer: out))
    }
}

// 確定したものだけを渡す。途中経過は書き換わるので、そこで拾うと誤爆する
// （実測: 「けい」が途中に出たのに、確定では消えた回があった）
let reader = Task {
    for try await result in transcriber.results where result.isFinal {
        let text = String(result.text.characters[...]).trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { emit("final", text) }
    }
}

do {
    try engine.start()
    try await analyzer.start(inputSequence: stream)
} catch {
    emit("error", "聞き始められません: \(error)")
    exit(1)
}
emit("ready")

// 親（Python）が閉じるまで聞き続ける
while true {
    try? await Task.sleep(for: .seconds(3600))
}
_ = await reader.result
