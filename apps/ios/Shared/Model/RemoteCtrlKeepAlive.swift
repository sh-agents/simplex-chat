//
//  RemoteCtrlKeepAlive.swift
//  SimpleX (iOS)
//
//  Copyright © 2026 SimpleX Chat. All rights reserved.
//

import ActivityKit
import AVFoundation
import Combine
import SwiftUI
import SimpleXChat

private let silenceSampleRate: Double = 44_100
private let silenceFrames: AVAudioFrameCount = 4_096

private enum RemoteCtrlKeepAliveError: Error {
    case silenceBuffer
}

private struct AudioSessionConfig {
    var category: AVAudioSession.Category
    var mode: AVAudioSession.Mode
    var options: AVAudioSession.CategoryOptions
    var prefersNoInterruptionsFromSystemAlerts: Bool
}

final class RemoteCtrlKeepAlive: ObservableObject {
    static let shared = RemoteCtrlKeepAlive()

    @Published private(set) var running = false
    private var sessionSubscription: AnyCancellable?
    private var savedAudioSession: AudioSessionConfig?
    private var audioEngine: AVAudioEngine?
    private var engineObserver: NSObjectProtocol?
    private var audioSessionObservers: [NSObjectProtocol] = []
    private var activityStateTask: Task<Void, Never>?

    func observeSession() {
        // a previous process could be terminated while connected, without ending its indicator
        endIndicator()
        sessionSubscription = ChatModel.shared.$remoteCtrlSession
            .map { session -> RemoteCtrlInfo? in
                if case let .connected(remoteCtrl, _) = session?.sessionState { remoteCtrl } else { nil }
            }
            .removeDuplicates { $0?.remoteCtrlId == $1?.remoteCtrlId }
            .sink { [weak self] remoteCtrl in
                if let remoteCtrl {
                    self?.sessionConnected(remoteCtrl)
                } else {
                    self?.sessionEnded()
                }
            }
    }

    func disconnect() async {
        do {
            try await stopRemoteCtrlSession()
        } catch let error {
            logger.error("RemoteCtrlKeepAlive disconnect error: \(responseError(error))")
        }
    }

    func stop() {
        guard running else { return }
        running = false
        stopAudio()
        endIndicator()
    }

    private func sessionConnected(_ remoteCtrl: RemoteCtrlInfo) {
        // a call owns the audio session and keeps the app running until it ends
        if ChatModel.shared.activeCall == nil && startAudio() {
            running = true
            showIndicator(ChatModel.shared.notificationPreview == .hidden ? nil : remoteCtrl.deviceViewName)
        }
        AppDelegate.keepScreenOn(!running)
    }

    private func sessionEnded() {
        AppDelegate.keepScreenOn(false)
        guard running else { return }
        stop()
        if UIApplication.shared.applicationState == .background {
            // runs after the ended session is published, so the local session is loaded before the chat is suspended
            DispatchQueue.main.async {
                switchToLocalSession()
                if AppChatState.shared.value.canSuspend {
                    suspendChatInBackground()
                }
            }
        }
    }

    private func startAudio() -> Bool {
        let session = AVAudioSession.sharedInstance()
        savedAudioSession = AudioSessionConfig(
            category: session.category,
            mode: session.mode,
            options: session.categoryOptions,
            prefersNoInterruptionsFromSystemAlerts: session.prefersNoInterruptionsFromSystemAlerts
        )
        do {
            try session.setCategory(.playback, mode: .default, options: .mixWithOthers)
            try session.setPrefersNoInterruptionsFromSystemAlerts(true)
            try session.setActive(true)
            try playSilence()
        } catch let error {
            logger.error("RemoteCtrlKeepAlive audio error: \(error.localizedDescription)")
            stopAudio()
            return false
        }
        let nc = NotificationCenter.default
        audioSessionObservers = [
            nc.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: .main) { [weak self] notification in
                self?.audioInterrupted(notification)
            },
            nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: session, queue: .main) { [weak self] _ in
                self?.audioFailed("media services were reset")
            }
        ]
        return true
    }

    private func playSilence() throws {
        guard let format = AVAudioFormat(standardFormatWithSampleRate: silenceSampleRate, channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: silenceFrames) else {
            throw RemoteCtrlKeepAliveError.silenceBuffer
        }
        buffer.frameLength = silenceFrames
        // a new buffer is not guaranteed to be zero-filled
        for channel in UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList) {
            if let data = channel.mData {
                memset(data, 0, Int(channel.mDataByteSize))
            }
        }
        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        player.scheduleBuffer(buffer, at: nil, options: .loops)
        try engine.start()
        player.play()
        audioEngine = engine
        // the engine stops itself when the output route changes its format
        engineObserver = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            self?.restartAudio()
        }
    }

    private func audioInterrupted(_ notification: Notification) {
        guard let info = notification.userInfo,
              let typeValue = info[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: typeValue) else { return }
        switch type {
        case .began:
            // posted only when the app runs again after the system suspended it and deactivated its session
            if let reasonValue = info[AVAudioSessionInterruptionReasonKey] as? UInt,
               AVAudioSession.InterruptionReason(rawValue: reasonValue) == .appWasSuspended {
                restartAudio()
            }
        case .ended:
            // silent mixable audio does not compete with other audio, so it resumes without the shouldResume option
            restartAudio()
        @unknown default:
            break
        }
    }

    private func restartAudio() {
        guard running else { return }
        stopEngine()
        do {
            try AVAudioSession.sharedInstance().setActive(true)
            try playSilence()
        } catch let error {
            audioFailed(error.localizedDescription)
        }
    }

    private func audioFailed(_ reason: String) {
        guard running else { return }
        logger.error("RemoteCtrlKeepAlive audio stopped: \(reason)")
        let endTask = beginBGTask()
        Task {
            await disconnect()
            endTask()
        }
    }

    private func stopEngine() {
        if let engineObserver {
            NotificationCenter.default.removeObserver(engineObserver)
        }
        engineObserver = nil
        audioEngine?.stop()
        audioEngine = nil
    }

    private func stopAudio() {
        audioSessionObservers.forEach { NotificationCenter.default.removeObserver($0) }
        audioSessionObservers = []
        stopEngine()
        guard let saved = savedAudioSession else { return }
        savedAudioSession = nil
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setActive(false)
            try session.setCategory(saved.category, mode: saved.mode, options: saved.options)
            try session.setPrefersNoInterruptionsFromSystemAlerts(saved.prefersNoInterruptionsFromSystemAlerts)
        } catch let error {
            logger.error("RemoteCtrlKeepAlive audio session restore error: \(error.localizedDescription)")
        }
    }

    private func showIndicator(_ desktopName: String?) {
        if #available(iOS 16.1, *), startLiveActivity(desktopName) { return }
        NtfManager.shared.notifyRemoteCtrlConnected(desktopName)
    }

    @available(iOS 16.1, *)
    private func startLiveActivity(_ desktopName: String?) -> Bool {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return false }
        do {
            let activity = try Activity.request(
                attributes: RemoteCtrlActivityAttributes(desktopName: desktopName, connectedAt: .now),
                contentState: RemoteCtrlActivityAttributes.ContentState(),
                pushType: nil
            )
            // the system ends the activity after 8 hours, and the user can remove it
            activityStateTask = Task {
                for await state in activity.activityStateUpdates where state == .ended || state == .dismissed {
                    await MainActor.run {
                        if !Task.isCancelled {
                            NtfManager.shared.notifyRemoteCtrlConnected(desktopName)
                        }
                    }
                    break
                }
            }
            return true
        } catch let error {
            logger.error("RemoteCtrlKeepAlive Live Activity error: \(error.localizedDescription)")
            return false
        }
    }

    private func endIndicator() {
        NtfManager.shared.removeRemoteCtrlNotification()
        activityStateTask?.cancel()
        activityStateTask = nil
        if #available(iOS 16.1, *) {
            // read now, so that an activity started for the next session is not ended
            let activities = Activity<RemoteCtrlActivityAttributes>.activities
            if !activities.isEmpty {
                let endTask = beginBGTask()
                Task {
                    for activity in activities {
                        await activity.end(using: nil, dismissalPolicy: .immediate)
                    }
                    endTask()
                }
            }
        }
    }
}
