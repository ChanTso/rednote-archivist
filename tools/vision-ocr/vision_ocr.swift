import AppKit
import Foundation
import Vision

func emit(_ object: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

let args = CommandLine.arguments.dropFirst()
guard let imagePath = args.first else {
    emit(["status": "failed", "error": "missing image path"])
    exit(2)
}

let useLanguageCorrection = args.contains("--language-correction")
guard let image = NSImage(contentsOfFile: imagePath) else {
    emit(["status": "failed", "error": "cannot load image"])
    exit(3)
}

var rect = CGRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    emit(["status": "failed", "error": "cannot create CGImage"])
    exit(4)
}

let width = CGFloat(cgImage.width)
let height = CGFloat(cgImage.height)
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
let desiredLanguages = ["zh-Hans", "zh-Hant", "en-US"]
if let supportedLanguages = try? request.supportedRecognitionLanguages() {
    let filtered = desiredLanguages.filter { supportedLanguages.contains($0) }
    request.recognitionLanguages = filtered.isEmpty ? ["en-US"] : filtered
} else {
    request.recognitionLanguages = ["zh-Hans", "en-US"]
}
request.automaticallyDetectsLanguage = false
request.usesLanguageCorrection = useLanguageCorrection

let start = Date()
let handler = VNImageRequestHandler(url: URL(fileURLWithPath: imagePath), options: [:])
do {
    try handler.perform([request])
} catch {
    emit(["status": "failed", "error": "\(type(of: error)): \(error)"])
    exit(5)
}

var texts: [String] = []
var blocks: [[String: Any]] = []
for observation in request.results ?? [] {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let box = observation.boundingBox
    let x1 = box.minX * width
    let y1 = (1.0 - box.maxY) * height
    let x2 = box.maxX * width
    let y2 = (1.0 - box.minY) * height
    texts.append(candidate.string)
    blocks.append([
        "text": candidate.string,
        "score": candidate.confidence,
        "box": [Double(x1), Double(y1), Double(x2), Double(y2)]
    ])
}

emit([
    "status": "success",
    "text": texts.joined(separator: "\n"),
    "blocks": blocks,
    "elapsed_seconds": Date().timeIntervalSince(start),
    "width": Int(width),
    "height": Int(height),
    "language_correction": useLanguageCorrection
])
