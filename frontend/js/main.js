// Boots the UI shell: one store, one event stream, and a module per concern.
// A new concern (Captures, disk) is a new
// module mounted here, plus its resource name in RESOURCES if the server
// pushes it.

import { logOut } from './api.js';
import { mountCameraList } from './camera-list.js';
import { mountCaptures } from './captures.js';
import { mountCertificateAlerts } from './certificates.js';
import { mountDevices } from './devices.js';
import { mountDiskIndicator } from './disk.js';
import { decorateTileEncryption, warnOnPlainRtsp } from './encryption.js';
import { connectEvents } from './events.js';
import { decorateTileStatus } from './feed-status.js';
import { mountFullScreenOnSingleView, mountFullScreenOnTiles } from './full-screen.js';
import { mountManagementBar } from './management-bar.js';
import { mountMapView } from './map-view.js';
import { mountMultiView } from './multi-view.js';
import { mountPositionOnSingleView, mountPositionOnTiles } from './position.js';
import { mountPositionDemand } from './position-demand.js';
import { mountQualityOnSingleView, mountQualityOnTiles } from './quality.js';
import { mountRecordingOnTiles } from './recording.js';
import { mountSingleView } from './single-view.js';
import { mountMapOnSingleView } from './single-view-map.js';
import { mountSnapshotOnSingleView, mountSnapshotOnTiles } from './snapshot.js';
import { createStore } from './store.js';
import { decorateTiles } from './tile.js';
import { mountViewState } from './view-state.js';
import { mountViewSwitch } from './view-switch.js';

const RESOURCES = ['devices', 'cameras', 'settings', 'captures', 'disk', 'positions', 'fixed_positions'];

decorateTiles(decorateTileStatus);
decorateTiles(decorateTileEncryption);

const store = createStore();
const bar = document.querySelector('.management-bar');
mountViewState(store); // first, so every view starts from this browser's saved state
mountViewSwitch(document.querySelector('[data-view-switch]'), store);
mountManagementBar(bar, document.querySelector('[data-bar-toggle]'), store);
// The bar's one live region announces a moved Panel, Camera or Device.
const announce = (text) => { bar.querySelector('[data-moved]').textContent = text; };
mountCameraList(bar.querySelector('[data-panel="cameras"]'), store, { announce }); // its own Panel: Captures can sit above it
mountDevices(bar, store, { announce });
warnOnPlainRtsp(bar.querySelector('#add-camera'));
mountCaptures(document.querySelector('.captures'), store);
mountRecordingOnTiles(); // before Multi View creates its first tile
mountSnapshotOnTiles();
mountQualityOnTiles();
mountPositionOnTiles(store);
mountFullScreenOnTiles(store);
mountMultiView(document.querySelector('.multi-view'), store);
mountCertificateAlerts(bar, store);
mountSingleView(document.querySelector('.single-view'), store);
mountSnapshotOnSingleView(document.querySelector('.single-view'), store);
mountQualityOnSingleView(document.querySelector('.single-view'), store);
mountPositionOnSingleView(document.querySelector('.single-view'), store);
mountMapOnSingleView(document.querySelector('.single-view'), store);
mountFullScreenOnSingleView(document.querySelector('.single-view'), store);
mountMapView(document.querySelector('.map-view'), store);
mountPositionDemand(store);
mountDiskIndicator(document.querySelector('[data-disk]'), store);
connectEvents(store, RESOURCES);
document.querySelector('[data-log-out]').addEventListener('click', logOut);
