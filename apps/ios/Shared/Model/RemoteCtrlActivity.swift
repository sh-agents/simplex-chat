//
//  RemoteCtrlActivity.swift
//  SimpleX (iOS)
//
//  Copyright © 2026 SimpleX Chat. All rights reserved.
//

import ActivityKit
import AppIntents
import Foundation

@available(iOS 16.1, *)
struct RemoteCtrlActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {}

    var desktopName: String?
    var connectedAt: Date
}

@available(iOS 17.0, *)
struct DisconnectDesktopIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Disconnect desktop"
    static let isDiscoverable = false

    func perform() async throws -> some IntentResult {
        // the system performs LiveActivityIntent in the app process, the extension only needs the type for its button
        #if !LIVE_ACTIVITY_EXTENSION
        await RemoteCtrlKeepAlive.shared.disconnect()
        #endif
        return .result()
    }
}
