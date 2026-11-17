import Combine
import CoreLocation
import Foundation

/// Where the phone is, for range tests. Locations never leave the phone.
@MainActor
final class LocationService: NSObject, ObservableObject {
    @Published private(set) var location: CLLocation? = nil
    @Published private(set) var allowed = true
    @Published private(set) var running = false

    private let manager = CLLocationManager()

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyBest
        manager.distanceFilter = kCLDistanceFilterNone
        manager.activityType = .otherNavigation
    }

    /// Start getting fixes. background: keep going with the screen off (during a test).
    func start(background: Bool) {
        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .denied, .restricted:
            allowed = false
            return
        default:
            break
        }
        if background && LocationService.backgroundAllowed {
            manager.allowsBackgroundLocationUpdates = true
            manager.showsBackgroundLocationIndicator = true
            manager.pausesLocationUpdatesAutomatically = false
        }
        manager.startUpdatingLocation()
        running = true
    }

    func stop() {
        manager.stopUpdatingLocation()
        if LocationService.backgroundAllowed {
            manager.allowsBackgroundLocationUpdates = false
        }
        running = false
    }

    /// Setting allowsBackgroundLocationUpdates without the "location" background
    /// mode in Info.plist crashes the app, so check first.
    static var backgroundAllowed: Bool {
        let modes = Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") as? [String] ?? []
        return modes.contains("location")
    }

    fileprivate func update(_ loc: CLLocation) {
        location = loc
    }

    fileprivate func authorization(_ status: CLAuthorizationStatus) {
        allowed = !(status == .denied || status == .restricted)
        if running && allowed {
            manager.startUpdatingLocation()
        }
    }
}

extension LocationService: CLLocationManagerDelegate {
    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let last = locations.last else { return }
        MainActor.assumeIsolated { self.update(last) }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        MainActor.assumeIsolated { self.authorization(status) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        // Temporary failures (no fix yet) are normal; keep waiting.
    }
}
