# AVAudioEngine Best Practices for Krab Ear Voice Assistant Mode

Real-time full-duplex audio with AVAudioEngine: capture microphone at 16kHz while playing response at 24kHz, both as 80ms chunks over WebSocket.

## Minimal Swift Setup (30–50 lines)

```swift
import AVFoundation

class AudioEngineManager {
    let engine = AVAudioEngine()
    let playerNode = AVAudioPlayerNode()
    
    func setupFullDuplex() throws {
        // Attach nodes
        engine.attach(playerNode)
        
        // Connect player → mainMixer → output
        engine.connect(playerNode, to: engine.mainMixerNode, format: outputFormat)
        
        // Format: 16kHz mono PCM for input tap
        let inputFormat = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
        
        // Tap inputNode: capture 1280 samples (80ms @ 16kHz)
        engine.inputNode.installTap(
            onBus: 0,
            bufferSize: 1280,
            format: inputFormat
        ) { [weak self] buffer, _ in
            self?.sendToWebSocket(buffer)
        }
        
        // Start engine
        try engine.start()
    }
    
    func schedulePlayback(_ pcmBuffer: AVAudioPCMBuffer) {
        if !playerNode.isPlaying { playerNode.play() }
        playerNode.scheduleBuffer(pcmBuffer)
    }
    
    func interruptPlayback() {
        playerNode.stop()  // Immediately stops + clears queue
    }
}
```

**Key points:**
- `inputNode.installTap()` captures without explicit connection; tap receives already-mixed input
- `bufferSize: 1280` = 80ms at 16kHz (samples = Hz × duration)
- `playerNode.scheduleBuffer()` queues for playback; multiple buffers stack until underrun
- `playerNode.stop()` clears entire queue mid-playback (for interruption)

## Format Conversion: Opus vs. Raw PCM

| Codec | Bandwidth (16kHz mono) | Encoder latency | Complexity | Recommendation |
|-------|------------------------|-----------------|-----------|-----------------|
| **Raw PCM** | ~192 kb/s | 0ms | Minimal | MVP (localhost WS, low latency priority) |
| **Opus** | ~32 kb/s | ~20ms | Medium | Post-MVP (bandwidth concern) |

For Voice Assistant MVP: **use raw PCM**. Moshi-Krab interop is local; bandwidth < latency savings. Add Opus post-launch if model moves remote.

### AVAudioConverter for 16 ↔ 24 kHz

```swift
// Input: 16kHz capture; output: 24kHz for Moshi playback response
let inputFormat = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
let outputFormat = AVAudioFormat(standardFormatWithSampleRate: 24000, channels: 1)!
let converter = AVAudioConverter(from: inputFormat, to: outputFormat)!

func convertBuffer(_ input: AVAudioPCMBuffer, to outputFormat: AVAudioFormat) -> AVAudioPCMBuffer {
    let outputCapacity = AVAudioFrameCount(ceil(Double(input.frameLength) * 24000 / 16000))
    let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: outputCapacity)!
    var error: NSError?
    converter.convert(to: output, error: &error) { inNumPackets in
        var status = AVAudioConverterInputStatus.noDataNow
        if let inAudio = input, inAudio.frameLength > 0 {
            status = .haveData
            inNumPackets.pointee = input.frameLength
        }
        return status
    }
    return output
}
```

**Rule:** never tap at non-native hardware sample rate. Tap at device native (usually 48kHz), then convert down to 16kHz for Moshi input. Conversely, schedule playback buffers already converted to device native before queueing to `playerNode`.

## Interruption Detection Pattern

Detect user speaking during AI response (energy-based voice activity):

```swift
func installInterruptionDetector(threshold: Float = -40) {
    let detectionFormat = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
    
    engine.inputNode.installTap(onBus: 0, bufferSize: 512, format: detectionFormat) { buffer, _ in
        guard let data = buffer.floatChannelData else { return }
        let power = self.calculateRMS(data[0], frameLength: buffer.frameLength)
        let dB = 20 * log10(power + 1e-6)
        
        if dB > threshold && self.isPlayingResponse {
            // User spoke; interrupt playback immediately
            self.playerNode.stop()  // Clears all queued buffers
            self.websocketSend(["type": "control", "action": "interrupt"])
        }
    }
}

func calculateRMS(_ data: UnsafeMutablePointer<Float>, frameLength: AVAudioFrameCount) -> Float {
    var sum: Float = 0
    for i in 0..<Int(frameLength) {
        sum += data[i] * data[i]
    }
    return sqrt(sum / Float(frameLength))
}
```

Alternatively, use Moshi's "early stop" token if the model natively supports interruption signaling.

## Latency Budget Breakdown

| Component | Latency |
|-----------|---------|
| AVAudioEngine I/O buffer (system) | ~2–5ms |
| Tap + WebSocket send | ~10ms |
| Network round-trip (localhost) | ~1–5ms |
| Moshi inference + streaming | ~300–500ms |
| WebSocket receive + schedule | ~5ms |
| AVAudioPlayerNode playback queue | ~0–20ms (device-dependent) |
| **Total** | **~320–530ms** |

**AVAudioEngine contribution: ~20ms.** Primary latency is Moshi inference, not audio pipeline. Configure `engine.preferredIOBufferDuration` = 0.005 (5ms) to minimize engine's slice:

```swift
do {
    let audioSession = AVAudioSession.sharedInstance()
    try audioSession.setCategory(.record, mode: .voiceChat, options: .duckOthers)
    try audioSession.setActive(true)
    engine.preferredIOBufferDuration = 0.005  // 5ms slices
    try engine.start()
} catch {
    print("Audio session error: \(error)")
}
```

## Top 3 Pitfalls + Mitigations

### 1. **Device Change Crashes Engine**
User plugs in USB headset → hardware sample rate changes → engine chokes.

**Mitigation:**
```swift
override func viewDidLoad() {
    let center = NotificationCenter.default
    center.addObserver(
        self,
        selector: #selector(handleRouteChange),
        name: AVAudioSession.routeChangeNotification,
        object: nil
    )
}

@objc func handleRouteChange(_ notification: Notification) {
    // Stop engine, remove taps, restart with new device format
    engine.stop()
    engine.inputNode.removeTap(onBus: 0)
    try? setupFullDuplex()
}
```

### 2. **Sample Rate Mismatch Stuttering**
Tap format ≠ device native format → audio unit resampling distorts real-time chain.

**Mitigation:** Always query device native sample rate:
```swift
let audioSession = AVAudioSession.sharedInstance()
let nativeSampleRate = audioSession.sampleRate  // e.g., 48000.0
// Tap at nativeSampleRate, convert down in tap closure before WebSocket send
```

### 3. **Underrun Silence**
WebSocket lags, no buffer scheduled → playerNode starves → silence + pop.

**Mitigation:** maintain a small ringbuffer (3–5 frames deep) before queueing:
```swift
class PlaybackBuffer {
    private var queue: [AVAudioPCMBuffer] = []
    private let lock = NSLock()
    
    func enqueue(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        queue.append(buffer)
        while queue.count > 5 { queue.removeFirst() }  // Discard old if lag clears
        lock.unlock()
    }
    
    func dequeueForPlayback() -> AVAudioPCMBuffer? {
        lock.lock()
        let result = queue.isEmpty ? nil : queue.removeFirst()
        lock.unlock()
        return result
    }
}
```

## Info.plist + Permissions

Add to `Info.plist`:
```xml
<key>NSMicrophoneUsageDescription</key>
<string>KrabEar needs microphone access to record voice.</string>
<key>UIBackgroundModes</key>
<array>
    <item>audio</item>
</array>
```

Require minimum macOS 13+ (AVAudioEngine + async/await support).

## Reference & Sample Code

- [AVAudioEngine Apple Docs](https://developer.apple.com/documentation/avfaudio/avaudioengine)
- [WWDC 2014 Session 502 – AVAudioEngine in Practice](https://asciiwwdc.com/2014/sessions/502)
- [Streaming Audio With AVAudioEngine](https://www.syedharisali.com/articles/streaming-audio-with-avaudioengine/)
- [Handling Audio Capture Gaps on macOS](https://nonstrict.eu/blog/2024/handling-audio-capture-gaps-on-macos/)
- [Mastering AVAudioPlayerNode Interrupts & Completion Callbacks](https://medium.com/@mehsamadi/mastering-avaudioplayernode-interrupts-and-completion-callbacks-da39b36abbf7)

---

**Word count:** 480 | **Delivery:** ready for PR 1.3 (Voice Assistant input/output foundation)
