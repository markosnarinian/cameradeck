# CameraDeck

A passenger-friendly camera console for Raspberry Pi: local-first, no cloud, built on Picamera2 and libcamera. Supports cameras exposed by that stack, including Arducam B0569 / IMX415 and Raspberry Pi Camera Module 3 / 3 NoIR.

## Design

- One camera owner and shared 640px MJPEG preview, not one camera pipeline per browser.
- ISP-scaled YUV streams and hardware H.264 recording; MP4 muxing without transcoding.
- Full-sensor JPEG stills outside recording; video-resolution snapshots during recording.
- Controls discovered from the camera, including an advanced editor for every advertised control.
- Thumbnail-first library, intermediate-size still viewer, explicit original download, seekable video.
- Optional 3×3 sharpness grid. Relative detail/contrast scores are not a calibrated focus measurement; motion, light, noise and texture affect them.
- Large touch targets, mobile layout, explicit recording state and errors. Intended for a passenger, never a driver.

Implementation and hardware verification notes will be added with the application.
