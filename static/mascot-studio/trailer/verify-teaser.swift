// Run: swift trailer/verify-teaser.swift <teaser.mp4> [expected-seconds]
import Foundation
import AVFoundation
import AppKit
import CryptoKit

guard (2...3).contains(CommandLine.arguments.count), FileManager.default.fileExists(atPath: CommandLine.arguments[1]) else {
    fputs("Provide an existing teaser MP4 and optional expected duration (default 12).\n", stderr)
    exit(1)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let asset = AVURLAsset(url: url)
let duration = asset.duration.seconds
guard let expectedDuration = Double(CommandLine.arguments.count == 3 ? CommandLine.arguments[2] : "12"),
      expectedDuration > 0 else {
    fputs("Expected duration must be a positive number.\n", stderr)
    exit(1)
}
guard let video = asset.tracks(withMediaType: .video).first,
      let audio = asset.tracks(withMediaType: .audio).first else {
    fputs("The teaser must contain both video and audio tracks.\n", stderr)
    exit(1)
}
let size = video.naturalSize
precondition(abs(duration - expectedDuration) < 0.1, "Incorrect duration")
precondition(size.width == 1280 && size.height == 720, "Incorrect frame size")
precondition(abs(video.nominalFrameRate - 24) < 0.1, "Incorrect frame rate")
let audioDescription = audio.formatDescriptions.first as! CMAudioFormatDescription
let audioFormat = CMAudioFormatDescriptionGetStreamBasicDescription(audioDescription)!.pointee
precondition(audioFormat.mChannelsPerFrame == 2 && audioFormat.mSampleRate == 48000, "Expected stereo 48 kHz")

let videoReader = try AVAssetReader(asset: asset)
let videoOutput = AVAssetReaderTrackOutput(track: video, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
])
videoReader.add(videoOutput)
videoReader.startReading()
var decodedFrames = 0
var previousTime = -1.0
while let frame = videoOutput.copyNextSampleBuffer() {
    let frameTime = CMSampleBufferGetPresentationTimeStamp(frame).seconds
    precondition(frameTime > previousTime, "Non-increasing frame timestamps")
    precondition(CMSampleBufferGetImageBuffer(frame) != nil, "Undecodable video frame")
    previousTime = frameTime
    decodedFrames += 1
}
precondition(videoReader.status == .completed, "Video decoding failed")
precondition(decodedFrames == Int((expectedDuration * 24).rounded()), "Missing video frames")

let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero
var hashes: [String] = []
let reviewTimes = expectedDuration == 14 ? [0.9, 3.35, 5.72, 7.85, 9.96, 12.25] : [0.9, 4.1, 8.25, 10.5]
for seconds in reviewTimes {
    let image = try generator.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 600), actualTime: nil)
    let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])!
    let name = String(format: "verified-%.2f.png", seconds)
    try png.write(to: url.deletingLastPathComponent().appendingPathComponent(name))
    hashes.append(SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined())
}
precondition(Set(hashes).count == hashes.count, "Repeated still instead of animation")

let reader = try AVAssetReader(asset: asset)
let output = AVAssetReaderTrackOutput(track: audio, outputSettings: [
    AVFormatIDKey: kAudioFormatLinearPCM,
    AVLinearPCMBitDepthKey: 32,
    AVLinearPCMIsFloatKey: true,
    AVLinearPCMIsNonInterleaved: false
])
reader.add(output)
reader.startReading()
var peak: Float = 0
var sumSquares: Double = 0
var samples = 0
var pausePeak: Float = 0
while let buffer = output.copyNextSampleBuffer() {
    guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
    let length = CMBlockBufferGetDataLength(block)
    var data = Data(count: length)
    data.withUnsafeMutableBytes { pointer in
        _ = CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: pointer.baseAddress!)
    }
    data.withUnsafeBytes { bytes in
        for sample in bytes.bindMemory(to: Float.self) {
            let seconds = Double(samples) / 96000
            if seconds >= 9.5 && seconds < 9.85 { pausePeak = max(pausePeak, abs(sample)) }
            peak = max(peak, abs(sample))
            sumSquares += Double(sample) * Double(sample)
            samples += 1
        }
    }
}
precondition(reader.status == .completed, "Audio decoding failed")
precondition(samples > Int(48000 * (expectedDuration - 1)), "Missing or short soundtrack")
precondition(abs(Double(samples) / 96000 - duration) < 0.1, "Incorrect decoded audio duration")
precondition(peak > 0.05 && peak < 0.99, "Silent or clipped audio")
if expectedDuration == 14 { precondition(pausePeak < 0.001, "Intentional quiet beat is missing") }
let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
let report: [String: Any] = [
    "file": url.lastPathComponent,
    "seconds": duration,
    "width": size.width,
    "height": size.height,
    "fps": video.nominalFrameRate,
    "decodedVideoFrames": decodedFrames,
    "audioChannels": audioFormat.mChannelsPerFrame,
    "audioSampleRate": audioFormat.mSampleRate,
    "bytes": attributes[.size]!,
    "videoBitrate": video.estimatedDataRate,
    "audioPeakDBFS": 20 * log10(Double(peak)),
    "audioRMSDBFS": 20 * log10(sqrt(sumSquares / Double(samples))),
    "differentDecodedFrames": Set(hashes).count,
    "pausePeakDBFS": 20 * log10(max(Double(pausePeak), 1e-9))
]
let json = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
try json.write(to: url.deletingLastPathComponent().appendingPathComponent("verification.json"))
print(String(data: json, encoding: .utf8)!)
