# HAR-80 route B certification

This producer line uses route B for the certified surface split.

- The vendored `groundtruth_mcp` wheel is the certified Python runtime surface
  consumed by `gt-harness` (`groundtruth.runtime.*` imports).
- This Groundtruth source tree certifies the `gt-index` binary and its
  framework-resolution overlays, including `framework_surface_resolution_v1`.
- The source tree is not claimed to provide the Python runtime adapter package;
  harness import parity is tested against the pinned wheel instead.

Provenance: HAR-80 route declaration packet `har80-route-b-701a8472` on
`refs/heads/gt-review-inbox`, with the producer source based on
`db9daf9ecf3a6ec1c92c40fba214ee66e4d09d14`.
