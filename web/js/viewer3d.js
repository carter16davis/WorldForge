/* The 3D stage.
 *
 * Scene frame: +X east, +Y up, -Z true north, one unit = one metre. That is
 * glTF's own convention, so `building.glb` drops in without a fix-up rotation
 * and the numbers on screen are the numbers in placement.json.
 *
 * Three things are deliberate:
 *   - The footprint outline does NOT follow the transform. The model rotates
 *     against a fixed real-world footprint, which is what makes "align the
 *     heading" a thing you can see rather than a number you guess at.
 *   - Scale is shown, not asserted: a 10 m grid and a 1.8 m figure sit in the
 *     scene, so a wrong metersPerModelUnit is obvious at a glance.
 *   - The 2426 treatment only touches materials, light and scatter. Geometry,
 *     coordinates and scale are identical in both eras, and the export says so.
 */

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { bearingDelta, pointInRing, toScene } from "./geo.js";

const DEG = Math.PI / 180;

const STATUS_COLOR = {
  verified:   0x45c08a,
  partial:    0xe8a33d,
  uncaptured: 0x566074,
  synthetic:  0xa26cf0,
};

/* Era endpoints. Everything between is a straight lerp driven by one scalar. */
const ERAS = [
  {   // 2026
    background: 0x0d1017,
    fogColor: 0x11161f,
    fogStrength: 0.17,   // multiples of the framing distance; see _applyEra
    hemi: { sky: 0x9fb6d4, ground: 0x2a3140, intensity: 1.15 },
    sun: { color: 0xfff4e0, intensity: 2.2, dir: [0.45, 0.85, 0.35] },
    groundColor: 0x1b2230,
    buildingTint: 0xffffff,
    roughness: 0.72,
    metalness: 0.18,
    emissive: 0x000000,
    growth: 0.0,
    dust: 0.0,
  },
  {   // 2426
    background: 0x180f09,
    fogColor: 0x2b190d,
    fogStrength: 0.62,
    hemi: { sky: 0xc07a38, ground: 0x2a1a10, intensity: 0.85 },
    sun: { color: 0xff9440, intensity: 1.45, dir: [0.7, 0.35, 0.25] },
    groundColor: 0x2a2015,
    buildingTint: 0x8c6a4e,
    roughness: 0.96,
    metalness: 0.04,
    emissive: 0x160600,
    growth: 1.0,
    dust: 1.0,
  },
];

const lerp = (a, b, t) => a + (b - a) * t;

export class Viewer {
  constructor(host) {
    this.host = host;
    this.era = 0;
    this.coverageShading = false;
    this.placement = null;
    this.coverage = null;
    this.disposed = false;

    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    host.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2(0x11161f, 0.001);

    this.camera = new THREE.PerspectiveCamera(46, 1, 0.5, 12000);
    this.camera.position.set(260, 190, 330);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.maxPolarAngle = 1.505;     // stay above the horizon
    this.controls.minDistance = 8;
    this.controls.maxDistance = 4000;

    this._buildStaticScene();

    this.building = new THREE.Group();
    this.scene.add(this.building);

    // Nested inside `building` so the declared up-axis correction is applied
    // before heading and scale, and so `_clear` can empty the mesh without
    // discarding that correction.
    this.frameFix = new THREE.Group();
    this.building.add(this.frameFix);

    this.footprintGroup = new THREE.Group();   // fixed: the model rotates against it
    this.scene.add(this.footprintGroup);

    this.scatterGroup = new THREE.Group();
    this.scene.add(this.scatterGroup);

    this._observeSize();
    this._applyEra(0);
    this._loop = this._loop.bind(this);
    this.renderer.setAnimationLoop(this._loop);
  }

  /* ───────────────────────── static furniture ───────────────────────── */

  _buildStaticScene() {
    this.hemi = new THREE.HemisphereLight(0x9fb6d4, 0x2a3140, 1.15);
    this.scene.add(this.hemi);

    this.sun = new THREE.DirectionalLight(0xfff4e0, 2.2);
    this.sun.position.set(450, 850, 350);
    this.scene.add(this.sun);

    this.groundMat = new THREE.MeshStandardMaterial({
      color: 0x1b2230, roughness: 1, metalness: 0,
    });
    this.ground = new THREE.Mesh(new THREE.CircleGeometry(3000, 72), this.groundMat);
    this.ground.rotation.x = -Math.PI / 2;
    this.ground.position.y = -0.05;          // keeps the grid from z-fighting
    this.scene.add(this.ground);

    this.grid = new THREE.GridHelper(2000, 200, 0x33405a, 0x232c3d);
    this.grid.material.transparent = true;
    this.grid.material.opacity = 0.5;
    this.scene.add(this.grid);

    this.compass = this._buildCompass();
    this.scene.add(this.compass);

    this.human = this._buildHuman();
    this.scene.add(this.human);

    this.dust = this._buildDust();
    this.scene.add(this.dust);
  }

  /** A north arrow lying on the ground, pointing at -Z. */
  _buildCompass() {
    const group = new THREE.Group();
    const mat = new THREE.MeshBasicMaterial({ color: 0xe8c46a, transparent: true, opacity: 0.85 });

    const shaft = new THREE.Mesh(new THREE.PlaneGeometry(1.4, 44), mat);
    shaft.rotation.x = -Math.PI / 2;
    shaft.position.set(0, 0.06, -22);
    group.add(shaft);

    const head = new THREE.Mesh(new THREE.ConeGeometry(5, 13, 3), mat);
    head.rotation.set(-Math.PI / 2, 0, 0);
    head.position.set(0, 0.06, -50);
    group.add(head);

    return group;
  }

  /** 1.8 m figure. Crude on purpose — it is a ruler, not a character. */
  _buildHuman() {
    const group = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({ color: 0xe8c46a, roughness: 0.6 });
    const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.22, 1.05, 4, 8), mat);
    body.position.y = 0.85;
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.16, 10, 8), mat);
    head.position.y = 1.65;
    group.add(body, head);
    group.position.set(0, 0, 0);
    return group;
  }

  _buildDust() {
    const count = 1400;
    const pos = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      const r = 40 + Math.random() * 420;
      const a = Math.random() * Math.PI * 2;
      pos[i * 3] = Math.cos(a) * r;
      pos[i * 3 + 1] = Math.random() * 150;
      pos[i * 3 + 2] = Math.sin(a) * r;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    const mat = new THREE.PointsMaterial({
      color: 0xd8a36a, size: 1.1, transparent: true, opacity: 0,
      depthWrite: false, sizeAttenuation: true,
    });
    const points = new THREE.Points(geo, mat);
    points.visible = false;
    return points;
  }

  /* ───────────────────────────── asset ───────────────────────────── */

  async setAsset({ modelUrl, placement, coverage }) {
    this.placement = placement;
    this.coverage = coverage;

    this._drawFootprint(placement);

    const gltf = await new GLTFLoader().loadAsync(modelUrl);
    if (this.disposed) return;

    this._clear(this.frameFix);
    const root = gltf.scene;

    this.meshes = [];
    root.traverse((obj) => {
      if (!obj.isMesh) return;

      // A photogrammetric model's whole value is its texture: it is the measured
      // appearance of the building, and the reason a reconstruction beats a
      // footprint extrusion. This used to replace every material with a flat
      // colour unconditionally, which threw that away and rendered a real scan
      // as a grey blob. So the texture is kept when there is one, and the flat
      // material is what the untextured prepared extrusion gets.
      const textured = !!obj.material?.map;
      if (textured) {
        obj.material.map.colorSpace = THREE.SRGBColorSpace;
        obj.material.roughness = 0.9;
        obj.material.metalness = 0.0;
        // RealityScan exports no normals, so they have to be computed either
        // way. On indexed geometry they come out smooth, which is what a
        // measured surface should look like — and it avoids tripling a
        // 400,000-triangle mesh to get the faceting the demo box wants.
        if (!obj.geometry.attributes.normal) obj.geometry.computeVertexNormals();
      } else {
        obj.geometry = obj.geometry.toNonIndexed();
        obj.geometry.computeVertexNormals();
        obj.material = new THREE.MeshStandardMaterial({
          color: new THREE.Color(placement?.appearance?.facadeColor || "#9aa3ad"),
          roughness: 0.72,
          metalness: 0.18,
          vertexColors: false,
        });
      }
      obj.userData.baseColor = obj.material.color.clone();
      obj.userData.textured = textured;
      this.meshes.push(obj);
    });

    this.frameFix.add(root);
    this.applyTransform(placement.transform);
    this._buildScatter(placement);
    this._applyEra(this.era);
    this.setCoverageShading(this.coverageShading);
    this.frame();
    return this.stats();
  }

  applyTransform(transform = {}) {
    const heading = Number(transform.headingDegrees ?? 0);
    const scale = Number(transform.metersPerModelUnit ?? 1);
    const offset = Number(transform.verticalOffsetMeters ?? 0);

    // A mesh that declares itself Z-up is in the ENU frame photogrammetry and
    // GIS tools emit: +X east, +Y north, +Z up. Lay it down onto glTF's Y-up
    // before anything else touches it, so heading and scale below mean the same
    // thing for both conventions. `reconstruction.anchor` converts the geometry
    // itself; this is what keeps a mesh that skipped that step from appearing on
    // its side rather than appearing wrong in a way nobody notices.
    this.frameFix.rotation.x = transform.upAxis === "Z" ? -Math.PI / 2 : 0;

    // -Z is north and heading is clockwise from north, so a positive heading
    // is a negative rotation about +Y.
    this.building.rotation.y = -heading * DEG;
    this.building.scale.setScalar(scale > 0 ? scale : 1);
    this.building.position.y = offset;
  }

  _drawFootprint(placement) {
    this._clear(this.footprintGroup);
    if (!placement?.footprint?.length) return;

    const origin = [placement.location.latitude, placement.location.longitude];
    const mat = new THREE.LineBasicMaterial({ color: 0x6fd3e8, transparent: true, opacity: 0.9 });

    const rings = [placement.footprint, ...(placement.holes || [])];
    this.footprintRings = [];
    for (const ring of rings) {
      const pts = ring.map((p) => {
        const [x, , z] = toScene(p, origin);
        return new THREE.Vector3(x, 0.12, z);
      });
      if (pts.length) pts.push(pts[0].clone());
      this.footprintGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), mat));
      this.footprintRings.push(ring.map((p) => {
        const [x, , z] = toScene(p, origin);
        return [x, z];
      }));
    }
  }

  /* ───────────────────────── era + scatter ───────────────────────── */

  /** Vegetation and debris, scattered where a building is not. */
  _buildScatter(placement) {
    this._clear(this.scatterGroup);
    this.scatter = null;
    if (!this.footprintRings?.length) return;

    const [outer, ...holes] = this.footprintRings;
    const xs = outer.map((p) => p[0]);
    const zs = outer.map((p) => p[1]);
    const pad = 70;
    const box = {
      x0: Math.min(...xs) - pad, x1: Math.max(...xs) + pad,
      z0: Math.min(...zs) - pad, z1: Math.max(...zs) + pad,
    };

    const spots = [];
    const target = 520;
    for (let tries = 0; tries < target * 14 && spots.length < target; tries++) {
      const x = lerp(box.x0, box.x1, Math.random());
      const z = lerp(box.z0, box.z1, Math.random());
      const inOuter = pointInRing(x, z, outer);
      const inHole = holes.some((h) => pointInRing(x, z, h));
      // the field inside the bowl, and the ground outside the walls — never the walls
      if (inHole || !inOuter) spots.push([x, z, inHole]);
    }
    if (!spots.length) return;

    const tree = new THREE.InstancedMesh(
      new THREE.ConeGeometry(1.5, 6, 6),
      new THREE.MeshStandardMaterial({ color: 0x4a6b3a, roughness: 0.95, flatShading: true }),
      spots.length,
    );
    const rock = new THREE.InstancedMesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshStandardMaterial({ color: 0x5b5347, roughness: 1, flatShading: true }),
      spots.length,
    );

    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const e = new THREE.Euler();
    this.scatterScales = [];
    spots.forEach(([x, z, inHole], i) => {
      const h = inHole ? 0.6 + Math.random() * 1.5 : 0.8 + Math.random() * 2.2;
      this.scatterScales.push(h);
      e.set(0, Math.random() * Math.PI * 2, 0);
      q.setFromEuler(e);
      m.compose(new THREE.Vector3(x, 0, z), q, new THREE.Vector3(0.001, 0.001, 0.001));
      tree.setMatrixAt(i, m);

      const s = 0.6 + Math.random() * 1.8;
      e.set(Math.random() * 0.5, Math.random() * Math.PI, Math.random() * 0.5);
      q.setFromEuler(e);
      m.compose(new THREE.Vector3(x + (Math.random() - 0.5) * 9, s * 0.3, z + (Math.random() - 0.5) * 9),
                q, new THREE.Vector3(0.001, 0.001, 0.001));
      rock.setMatrixAt(i, m);
    });

    tree.visible = rock.visible = false;
    this.scatterGroup.add(tree, rock);
    this.scatter = { tree, rock, spots };
  }

  _updateScatter(t) {
    if (!this.scatter) return;
    const { tree, rock, spots } = this.scatter;
    const visible = t > 0.02;
    tree.visible = rock.visible = visible;
    if (!visible) return;

    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const pos = new THREE.Vector3();
    const scale = new THREE.Vector3();

    for (let i = 0; i < spots.length; i++) {
      // Growth is staggered so vegetation creeps in rather than popping.
      const stagger = Math.min(1, Math.max(0, (t - (i % 100) / 260) * 1.6));
      tree.getMatrixAt(i, m);
      m.decompose(pos, q, scale);
      const s = Math.max(0.001, stagger * this.scatterScales[i]);
      m.compose(pos, q, scale.set(s, s, s));
      tree.setMatrixAt(i, m);

      rock.getMatrixAt(i, m);
      m.decompose(pos, q, scale);
      const rs = Math.max(0.001, stagger * 1.4);
      m.compose(pos, q, scale.set(rs, rs, rs));
      rock.setMatrixAt(i, m);
    }
    tree.instanceMatrix.needsUpdate = true;
    rock.instanceMatrix.needsUpdate = true;
  }

  setEra(t) {
    this.era = Math.min(1, Math.max(0, t));
    this._applyEra(this.era);
  }

  _applyEra(t) {
    const [a, b] = ERAS;
    const c = new THREE.Color();

    this.scene.background = c.clone().lerpColors(new THREE.Color(a.background), new THREE.Color(b.background), t);
    this.scene.fog.color.lerpColors(new THREE.Color(a.fogColor), new THREE.Color(b.fogColor), t);
    // FogExp2 attenuates as exp(-(distance * density)^2), so a fixed density is
    // meaningless without a scene scale: keep the haze proportional to how far
    // the camera was framed, and both a house and a stadium read the same.
    this.scene.fog.density = lerp(a.fogStrength, b.fogStrength, t) / (this.sceneScale || 400);

    this.hemi.color.lerpColors(new THREE.Color(a.hemi.sky), new THREE.Color(b.hemi.sky), t);
    this.hemi.groundColor.lerpColors(new THREE.Color(a.hemi.ground), new THREE.Color(b.hemi.ground), t);
    this.hemi.intensity = lerp(a.hemi.intensity, b.hemi.intensity, t);

    this.sun.color.lerpColors(new THREE.Color(a.sun.color), new THREE.Color(b.sun.color), t);
    this.sun.intensity = lerp(a.sun.intensity, b.sun.intensity, t);
    this.sun.position.set(
      lerp(a.sun.dir[0], b.sun.dir[0], t) * 1000,
      lerp(a.sun.dir[1], b.sun.dir[1], t) * 1000,
      lerp(a.sun.dir[2], b.sun.dir[2], t) * 1000,
    );

    this.groundMat.color.lerpColors(new THREE.Color(a.groundColor), new THREE.Color(b.groundColor), t);
    this.grid.material.opacity = lerp(0.5, 0.12, t);

    if (!this.coverageShading) {
      const tint = c.clone().lerpColors(new THREE.Color(a.buildingTint), new THREE.Color(b.buildingTint), t);
      for (const mesh of this.meshes || []) {
        mesh.material.color.copy(mesh.userData.baseColor).multiply(tint);
        mesh.material.roughness = lerp(a.roughness, b.roughness, t);
        mesh.material.metalness = lerp(a.metalness, b.metalness, t);
        mesh.material.emissive.lerpColors(new THREE.Color(a.emissive), new THREE.Color(b.emissive), t);
      }
    }

    this.dust.visible = t > 0.02;
    this.dust.material.opacity = lerp(0, 0.5, t);
    this._updateScatter(t);
  }

  /* ─────────────────────── coverage shading ─────────────────────── */

  /**
   * Repaint each triangle by which facade observed it.
   *
   * The point is the honesty requirement in AGENTS.md: a viewer must be able to
   * see which surfaces were measured and which were inferred. Facade assignment
   * is by outward normal — exact for an extruded footprint, approximate for a
   * photogrammetric mesh, which is why the legend says "nearest facade".
   */
  setCoverageShading(on) {
    this.coverageShading = !!on;
    if (!this.meshes?.length) return;

    if (!on) {
      for (const mesh of this.meshes) {
        mesh.material.vertexColors = false;
        mesh.material.needsUpdate = true;
      }
      this._applyEra(this.era);
      return;
    }

    const all = this.coverage?.facades || [];
    // Horizontal surfaces are their own claim. Ground-level photography never
    // sees a roof, so unless the report explicitly covers one, the roof is
    // uncaptured — shading it with the overall confidence would dress up the
    // single largest inferred surface as if it had been measured.
    const roof = all.find((f) => /^roof/i.test(f.name));
    const roofColor = new THREE.Color(STATUS_COLOR[roof?.status] ?? STATUS_COLOR.uncaptured);
    const facades = all.filter(
      (f) => f !== roof && Number.isFinite(f.headingDegrees),
    );

    for (const mesh of this.meshes) {
      const geo = mesh.geometry;
      const normal = geo.attributes.normal;
      const count = geo.attributes.position.count;
      const colors = new Float32Array(count * 3);
      const c = new THREE.Color();

      for (let i = 0; i < count; i++) {
        const nx = normal.getX(i);
        const ny = normal.getY(i);
        const nz = normal.getZ(i);

        if (Math.abs(ny) > 0.7 || !facades.length) {
          c.copy(roofColor);                       // roof and floor: no facade owns them
        } else {
          // -Z is north, +X is east: bearing = atan2(east, north)
          const bearing = (Math.atan2(nx, -nz) * 180) / Math.PI;
          let best = facades[0];
          let bestDelta = 999;
          for (const f of facades) {
            const d = bearingDelta(bearing, f.headingDegrees);
            if (d < bestDelta) { bestDelta = d; best = f; }
          }
          c.set(STATUS_COLOR[best.status] ?? STATUS_COLOR.uncaptured);
          // darken with distance from the facade's own bearing, so a wall that
          // only glances at a captured direction does not read as fully verified
          c.multiplyScalar(lerp(1, 0.55, Math.min(1, bestDelta / 90)));
        }
        colors[i * 3] = c.r;
        colors[i * 3 + 1] = c.g;
        colors[i * 3 + 2] = c.b;
      }

      geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      mesh.material.vertexColors = true;
      mesh.material.color.set(0xffffff);
      mesh.material.emissive.set(0x000000);
      mesh.material.roughness = 0.9;
      mesh.material.metalness = 0;
      mesh.material.needsUpdate = true;
    }
  }

  /* ───────────────────────────── view ───────────────────────────── */

  setGrid(on) { this.grid.visible = on; this.compass.visible = on; }
  setHuman(on) { this.human.visible = on; }

  frame() {
    const box = new THREE.Box3().setFromObject(this.building);
    if (box.isEmpty()) return;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const dist = (sphere.radius / Math.sin((this.camera.fov * DEG) / 2)) * 1.25;

    this.controls.target.copy(sphere.center);
    this.camera.position.copy(sphere.center).add(
      new THREE.Vector3(0.55, 0.42, 0.72).normalize().multiplyScalar(dist),
    );
    this.camera.near = Math.max(0.3, dist / 800);
    this.camera.far = dist * 30;
    this.camera.updateProjectionMatrix();

    // Atmosphere and drifting dust are sized off the framing distance too.
    this.sceneScale = dist;
    this.dust.scale.setScalar(dist / 340);
    this.dust.position.copy(sphere.center).setY(0);
    this._applyEra(this.era);

    // Park the ruler and the compass just clear of the building. Both are
    // reference furniture: inside a 280 m stadium footprint they would simply
    // be invisible.
    const clear = sphere.radius * 1.12;
    this.human.position.set(sphere.center.x + clear, 0, sphere.center.z + clear * 0.35);
    this.compass.position.set(sphere.center.x - clear, 0, sphere.center.z);
    this.compass.scale.setScalar(Math.max(1, sphere.radius / 90));
    this.controls.update();
  }

  stats() {
    const box = new THREE.Box3().setFromObject(this.building);
    if (box.isEmpty()) return null;
    const size = box.getSize(new THREE.Vector3());
    let triangles = 0;
    for (const mesh of this.meshes || []) {
      // Indexed geometry is kept for textured scans, where the position count
      // is the vertex count and says nothing about the triangle count.
      const geo = mesh.geometry;
      triangles += (geo.index ? geo.index.count : geo.attributes.position.count) / 3;
    }
    return {
      widthM: size.x, heightM: size.y, depthM: size.z,
      triangles: Math.round(triangles),
    };
  }

  /* ───────────────────────────── plumbing ───────────────────────────── */

  _clear(group) {
    for (const child of [...group.children]) {
      group.remove(child);
      child.traverse?.((o) => {
        o.geometry?.dispose?.();
        // Textures are not freed by disposing the material that holds them, and
        // a photogrammetric atlas is tens of megabytes of video memory. Before
        // scans were textured this leaked nothing; now switching assets a few
        // times would exhaust a laptop GPU.
        for (const material of [o.material].flat().filter(Boolean)) {
          material.map?.dispose?.();
          material.dispose?.();
        }
      });
    }
  }

  _observeSize() {
    const resize = () => {
      const { clientWidth: w, clientHeight: h } = this.host;
      if (!w || !h) return;
      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    };
    this._resizeObserver = new ResizeObserver(resize);
    this._resizeObserver.observe(this.host);
    resize();
  }

  _loop() {
    if (this.disposed) return;
    this.controls.update();
    if (this.dust.visible) {
      this.dust.rotation.y += 0.00022;         // barely-there drift
    }
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    this.disposed = true;
    this.renderer.setAnimationLoop(null);
    this._resizeObserver?.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
    this.host.replaceChildren();
  }
}
