# WorldForge — Photo to Map-Ready 3D Building

## Project summary

WorldForge turns building photos or video plus a street address into a game-ready 3D asset that can be placed correctly in the world of **Scorched Nebraska**. The hackathon demo uses a World Cup venue as its showcase and presents the building in two states: its recognizable 2026 appearance and a speculative Scorched Nebraska version set centuries later.

The primary target is the **Procedura AI: Photo to Map-Ready 3D Building** track. The project should first satisfy that track clearly. Additional track integrations are optional and must not weaken the core demo.

## Core user story

1. The user uploads overlapping building photos or a continuous video.
2. The user enters the building's street address.
3. WorldForge reconstructs or imports a textured 3D building mesh.
4. The address is converted into geographic coordinates.
5. The model is placed on an interactive map at the resolved location.
6. The user reviews and corrects scale, heading, and ground height.
7. The application exports a GLB model and placement metadata.
8. The demo can switch between the preserved 2026 venue and a Scorched Nebraska presentation.

## Definition of “map-ready”

The exported building must have:

- A portable textured model, preferably GLB/GLTF
- Latitude and longitude
- Ground elevation or an explicit elevation offset
- Heading relative to north
- Real-world scale in meters
- A documented ground anchor and model origin
- A bounding box or footprint
- A preview thumbnail
- Provenance for source images and generated content
- A lower-detail model if time permits

Before finalizing the format, ask the Procedura representatives for Scorched Nebraska's required model format, coordinate system, up axis, unit scale, origin convention, texture limits, polygon budget, collision requirements, and metadata schema.

## MVP

The minimum successful demo is:

- Upload photos/video or select a prepared example
- Enter an address
- Produce or load a reconstructed textured mesh
- Geocode the address
- Display the building at the corresponding map location
- Adjust heading, scale, and vertical offset
- Export `building.glb` and `placement.json`
- Demonstrate the complete path using one World Cup venue

The live demo must use a prepared reconstruction so it does not depend on cloud processing finishing during judging.

## Stretch features

Implement these only after the complete MVP works:

1. Coverage analysis and guided recapture
2. Evidence-aware texture completion
3. Observed versus inferred surface overlay
4. 2026-to-2426 appearance slider
5. Automatic footprint matching
6. Automatic model simplification and levels of detail
7. Collision mesh generation
8. GoDaddy ANS agents for source discovery and verification

## Evidence-aware reconstruction agent

The agent supports reconstruction without treating invented pixels as measurements.

### MVP agent behavior

- Inspect the input set for blur, poor lighting, and duplicate frames
- Estimate which sides and viewing angles are covered
- Identify missing or weakly covered areas
- Recommend specific additional captures
- Produce a coverage and confidence report
- Record image provenance

Example recommendation:

> The northwest corner is under-observed. Capture five overlapping photos while moving from the north facade toward the west facade.

### Optional generative completion

Generated views may be used only as low-confidence appearance assistance. They must not determine coordinates, footprint, scale, structural dimensions, or collision geometry.

If implemented:

- Begin with an initial reconstruction, camera estimates, depth, or map footprint
- Generate for explicit target camera poses
- Prefer texture completion over invention of entire unseen structures
- Mark synthesized regions in metadata
- Let the user compare observed and inferred content
- Allow generated content to be disabled
- Keep synthetic images separate from original evidence

Do not feed unconstrained text-to-image views directly into the authoritative reconstruction.

## World Cup presentation

The demo concept is **World Cup Venue Time Machine**.

- The present-day mode shows the reconstructed 2026 venue.
- The Scorched mode shows a speculative future treatment through materials, vegetation, atmospheric effects, decals, or overlays.
- A time slider provides a clear visual transition between the two modes.
- Venue metadata may include location, capacity, and tournament context, subject to reliable sourcing.

The World Cup theme is the demonstration and storytelling layer. The reusable product remains a general building-to-map ingestion pipeline.

## System architecture

```text
Photos/video ──> input validation ──> reconstruction/import ──> optimized GLB
       │                  │                    │                    │
       └────> coverage agent ──> confidence report                 │
                                                                    ├──> package export
Address ──> geocoder ──> lat/lon ──> map placement editor ─────────┘
                                      │
                                      └──> heading / scale / elevation
```

### Recommended components

- Front end: React and TypeScript
- 3D preview: Three.js or React Three Fiber
- Map: a provider compatible with 3D overlays and permitted by its license
- API: Python/FastAPI or a lightweight TypeScript service
- Asset format: GLB/GLTF
- Reconstruction: external service, local photogrammetry tool, or prepared capture
- Geocoding: provider selected according to availability and terms
- Metadata: versioned JSON

Avoid coupling the entire demo to one paid reconstruction service. Keep a prepared GLB path available.

## Export contract

```text
exports/<asset-id>/
├── building.glb
├── building-lod.glb        # optional
├── thumbnail.webp
├── placement.json
└── provenance.json
```

Suggested `placement.json`:

```json
{
  "schemaVersion": 1,
  "assetId": "venue-metlife-001",
  "name": "New York New Jersey Stadium",
  "sourceAddress": "1 MetLife Stadium Drive, East Rutherford, NJ 07073",
  "location": {
    "latitude": 40.8135,
    "longitude": -74.0745,
    "elevationMeters": 2.4
  },
  "transform": {
    "headingDegrees": 87,
    "metersPerModelUnit": 1,
    "verticalOffsetMeters": 0,
    "anchor": "ground-center",
    "upAxis": "Y"
  },
  "models": {
    "high": "building.glb",
    "low": "building-lod.glb"
  }
}
```

Suggested `provenance.json`:

```json
{
  "schemaVersion": 1,
  "sourceMedia": [],
  "reconstructionTool": "",
  "observedCoveragePercent": null,
  "syntheticCoveragePercent": 0,
  "syntheticUse": "none",
  "licenseNotes": "",
  "generatedAt": ""
}
```

## Three-person work split

All three people should agree on the export contract and demo flow before building independently. Each person owns one vertical area and must provide a stable interface to the others.

### Person 1 — Reconstruction and AI

**Primary outcome:** turn source media into a usable asset and explain its confidence.

Responsibilities:

- Prepare the photo/video ingestion flow
- Connect the selected reconstruction approach
- Export or convert the result to GLB
- Remove obvious noise and crop irrelevant surroundings
- Implement the coverage-analysis agent
- Detect blurry, duplicate, or low-value inputs
- Produce confidence and provenance metadata
- Prepare the known-good World Cup venue asset
- Investigate optional texture completion only after the base path works

Deliverables:

- `building.glb`
- Asset processing script or documented service call
- Coverage/confidence JSON
- Known-good fallback asset
- Before-and-after reconstruction media for the presentation

Definition of done:

- Another teammate can pass in the prepared input and receive a GLB plus metadata without manual undocumented steps.

### Person 2 — Geospatial placement and export

**Primary outcome:** place the model correctly and produce a Scorched Nebraska-ready package.

Responsibilities:

- Implement address geocoding
- Resolve or approximate ground elevation
- Obtain a building footprint when available
- Define coordinate, unit, axis, origin, and anchor conventions
- Build heading, scale, and vertical-offset calculations
- Match a model to a footprint where practical
- Implement `placement.json` generation and validation
- Generate an optional lower-detail asset
- Confirm requirements with Procedura representatives

Deliverables:

- Geocoding and placement module
- Placement JSON schema and validator
- Export packaging function
- Documented coordinate conversions
- At least one verified test location

Definition of done:

- Given an address and GLB, the module produces a validated package with an explainable location and transform.

### Person 3 — Product, viewer, and demo integration

**Primary outcome:** make the complete workflow understandable and compelling to judges.

Responsibilities:

- Build the upload and address-entry interface
- Build the map and 3D model preview
- Add manual controls for heading, scale, and ground offset
- Visualize coverage confidence and generated surfaces
- Implement the 2026-to-2426 presentation toggle or slider
- Connect the reconstruction and geospatial modules
- Handle loading, error, and fallback states
- Prepare the final demo script, slides, and submission materials
- Record a backup demo video

Deliverables:

- Integrated web application
- Placement editor
- Export action
- World Cup themed demo mode
- Demo script and backup recording

Definition of done:

- A first-time user can complete the prepared workflow without a developer explaining hidden steps.

## Integration contracts

### Reconstruction output

Person 1 hands Person 2 and Person 3:

```json
{
  "assetId": "string",
  "modelPath": "string",
  "thumbnailPath": "string",
  "bounds": {
    "width": 0,
    "length": 0,
    "height": 0
  },
  "confidenceReportPath": "string",
  "provenancePath": "string"
}
```

### Placement output

Person 2 hands Person 3:

```json
{
  "latitude": 0,
  "longitude": 0,
  "elevationMeters": 0,
  "headingDegrees": 0,
  "metersPerModelUnit": 1,
  "verticalOffsetMeters": 0,
  "anchor": "ground-center",
  "upAxis": "Y"
}
```

### UI updates

Person 3 returns user corrections in the same placement shape. Person 2's validator must accept and package those corrections without needing UI-specific fields.

## Work sequence

### Phase 1 — Contract and proof

Everyone:

- Confirm sponsor requirements
- Freeze the GLB and JSON interfaces
- Select the reconstruction and geocoding services
- Confirm the demo building and source-media rights

In parallel:

- Person 1 produces the first GLB
- Person 2 geocodes the address and creates sample placement JSON
- Person 3 loads any placeholder GLB in a local viewer

Milestone: a placeholder model appears in the viewer and a package can be exported.

### Phase 2 — Vertical integration

- Replace the placeholder with the reconstructed venue
- Connect address lookup to the placement editor
- Add transform controls
- Add validation and error handling
- Verify scale, orientation, and map position

Milestone: complete MVP workflow works from input selection to export.

### Phase 3 — Differentiation

- Add coverage analysis
- Add provenance and confidence visualization
- Add the 2026-to-2426 treatment
- Add one high-value secondary-track feature only if the MVP remains stable

Milestone: polished story with technically defensible AI use.

### Phase 4 — Submission

- Freeze features
- Test the prepared path from a clean start
- Test without network access where possible
- Record a backup demonstration
- Verify source licenses and attribution
- Rehearse within the presentation time limit

## Collaboration rules

- Commit small changes with clear messages.
- Do not change the shared JSON contracts without agreement from all three people.
- Keep a known-good demo branch or tag.
- Store large generated assets outside Git or use Git LFS if permitted.
- Never depend on an unfinished cloud job for the live demo.
- Add one example command or UI path for every processing step.
- Report blockers early and switch to the fallback path rather than silently expanding scope.
- Prioritize an end-to-end working product over isolated technical sophistication.

## Testing checklist

- Address resolves to the expected location
- Model is upright and faces the intended direction
- Model scale is plausible relative to its footprint
- Model sits on the ground instead of floating or sinking
- GLB loads in a clean browser session
- Exported JSON passes schema validation
- Missing network services produce a helpful fallback
- Observed and synthetic content are distinguishable
- Source license and attribution are recorded
- Prepared demo completes within the judging time

## Secondary-track strategy

The main submission targets Procedura. If the core is complete, the strongest additional company track is **GoDaddy: Best Use of ANS**.

An ANS extension could use separate agents to:

- Discover candidate building media
- Verify address and building identity
- Check licensing and provenance
- Collect footprint and elevation evidence
- Communicate a structured evidence package

Do not enter a secondary track unless its required technology is visibly used in the working product. Avoid reframing the same shallow feature for unrelated tracks.

## Demo narrative

> WorldForge preserves real buildings as spatially anchored assets. Starting with photographs and an address, it reconstructs a portable model, verifies what was truly observed, places the building at its real coordinates, and exports a package for Scorched Nebraska. Our World Cup demonstration carries a 2026 venue into the simulated world of 2426 while preserving the provenance and uncertainty of the original reconstruction.

## Final success criteria

The project succeeds when judges can see that:

1. Real source media became a usable 3D model.
2. An address became a defensible spatial placement.
3. The user can review and correct uncertain transforms.
4. The output is portable and documented.
5. AI improves coverage assessment without disguising invented content as measured reality.
6. The result has a clear route into Scorched Nebraska.
