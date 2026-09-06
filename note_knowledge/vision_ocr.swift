import Foundation
import ImageIO
import Vision

struct OCRPayload: Codable {
    let rawText: String
    let lowConfidenceSegments: [String]

    enum CodingKeys: String, CodingKey {
        case rawText = "原始提取文本"
        case lowConfidenceSegments = "低置信度片段"
    }
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: vision_ocr.swift <image-path>\n".utf8))
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard
    let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
    let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
else {
    FileHandle.standardError.write(Data("unable to decode image\n".utf8))
    exit(3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]

do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let observations = (request.results ?? []).sorted { left, right in
        let verticalDifference = abs(left.boundingBox.midY - right.boundingBox.midY)
        if verticalDifference > 0.02 {
            return left.boundingBox.midY > right.boundingBox.midY
        }
        return left.boundingBox.minX < right.boundingBox.minX
    }
    var lines: [String] = []
    var lowConfidence: [String] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let value = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.isEmpty { continue }
        lines.append(value)
        if candidate.confidence < 0.6 { lowConfidence.append(value) }
    }
    let payload = OCRPayload(rawText: lines.joined(separator: "\n"), lowConfidenceSegments: lowConfidence)
    let data = try JSONEncoder().encode(payload)
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("Vision OCR failed: \(error)\n".utf8))
    exit(4)
}
