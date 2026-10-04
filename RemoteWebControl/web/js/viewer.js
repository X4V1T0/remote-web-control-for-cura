// 3D preview: build volume + the job's models. Printer coordinates everywhere (Z up, mm, origin at the
// centre of the build plate), exactly like the API, so job matrices are applied as they are.

import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";

// Scene colours per theme (see theme.js). "light" is Cura's own viewport: light grey plate,
// blue build volume outline and the yellow of Cura's generic PLA for the model.
const PALETTES = {
  dark: {
    background: 0x11141a, plate: 0x2b3242, grid: 0x394155, edges: 0x6b7489,
    disallowed: 0x7a2f35, disallowedOpacity: 0.7, marker: 0x9aa3b5, model: 0xff8a26, modelBad: 0xff5d5d,
    selection: 0xffffff,
  },
  light: {
    background: 0xfafafa, plate: 0xe4e4e4, grid: 0xc0c1c2, edges: 0x3282ff,
    disallowed: 0x000000, disallowedOpacity: 0.16, marker: 0x6c6c6c, model: 0xffc924, modelBad: 0xda1e28,
    selection: 0x196ef0, // Cura's selection outline.
  },
};

function currentPalette() {
  return PALETTES[document.documentElement.dataset.theme] || PALETTES.dark;
}

// Decodes the CRM1 preview mesh (see README): magic, flags, count, float32 vertices.
export function decodeMesh(buffer) {
  const view = new DataView(buffer);
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
  if (magic !== "CRM1") throw new Error("Unknown mesh format");
  const flags = view.getUint32(4, true);
  const count = view.getUint32(8, true);
  const positions = new Float32Array(buffer, 12, count * 9);
  return { positions, count, decimated: (flags & 1) === 1 };
}

// Row-major 16 numbers (API) -> THREE.Matrix4 (Matrix4.set takes row-major arguments).
export function toMatrix4(list) {
  return new THREE.Matrix4().set(...list);
}

export function fromMatrix4(matrix) {
  return matrix.clone().transpose().toArray(); // toArray() is column-major.
}

export class Viewer {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "low-power" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    container.appendChild(this.renderer.domElement);

    // Shared by every part of the build volume, so a theme change only recolours them.
    this.materials = {
      plate: new THREE.MeshBasicMaterial(),
      grid: new THREE.LineBasicMaterial(),
      edges: new THREE.LineBasicMaterial(),
      disallowed: new THREE.MeshBasicMaterial({ transparent: true }),
      marker: new THREE.MeshBasicMaterial(),
      selection: new THREE.LineBasicMaterial(),
    };
    this.onThemeChange = () => this.applyTheme();
    window.addEventListener("rwc-themechange", this.onThemeChange);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 1, 10000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = false;
    this.controls.addEventListener("change", () => this.render());

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x303040, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.8);
    sun.position.set(-0.6, -1, 1.4);
    this.scene.add(sun);

    this.volume = new THREE.Group();
    this.scene.add(this.volume);

    this.objects = new Map(); // object id -> { mesh, key, fits }
    this.geometries = new Map(); // file key -> BufferGeometry, shared by the copies of a model
    this.selectedId = null;
    this.onSelect = null; // Called with the id of the model tapped in the view.
    this.selectionBox = new THREE.Box3Helper(new THREE.Box3(), 0xffffff);
    this.selectionBox.material = this.materials.selection;
    this.selectionBox.visible = false;
    this.scene.add(this.selectionBox);
    this.listenForTaps();

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.applyTheme();
    this.resize();
  }

  applyTheme() {
    const palette = currentPalette();
    this.renderer.setClearColor(palette.background);
    this.materials.plate.color.setHex(palette.plate);
    this.materials.grid.color.setHex(palette.grid);
    this.materials.edges.color.setHex(palette.edges);
    this.materials.disallowed.color.setHex(palette.disallowed);
    this.materials.disallowed.opacity = palette.disallowedOpacity;
    this.materials.marker.color.setHex(palette.marker);
    this.materials.selection.color.setHex(palette.selection);
    for (const entry of this.objects.values()) entry.mesh.material.color.setHex(entry.fits ? palette.model : palette.modelBad);
    this.render();
  }

  setPrinter(printer) {
    this.volume.clear();
    const { x: width, y: depth, z: height } = printer.build_volume;
    const elliptic = printer.shape === "elliptic";

    const plateShape = new THREE.Shape();
    if (elliptic) plateShape.absellipse(0, 0, width / 2, depth / 2, 0, Math.PI * 2);
    else plateShape.moveTo(-width / 2, -depth / 2).lineTo(width / 2, -depth / 2).lineTo(width / 2, depth / 2).lineTo(-width / 2, depth / 2);
    const plate = new THREE.Mesh(new THREE.ShapeGeometry(plateShape, 48), this.materials.plate);
    plate.position.z = -0.2;
    this.volume.add(plate);

    // Grid every 10 mm, clipped to the plate for rectangular printers.
    if (!elliptic) {
      const points = [];
      for (let x = -width / 2; x <= width / 2 + 0.01; x += 10) points.push(x, -depth / 2, 0, x, depth / 2, 0);
      for (let y = -depth / 2; y <= depth / 2 + 0.01; y += 10) points.push(-width / 2, y, 0, width / 2, y, 0);
      const grid = new THREE.BufferGeometry();
      grid.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
      this.volume.add(new THREE.LineSegments(grid, this.materials.grid));
    }

    const box = new THREE.BoxGeometry(width, depth, height);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(box), this.materials.edges);
    edges.position.z = height / 2;
    if (!elliptic) this.volume.add(edges);

    for (const polygon of printer.disallowed_areas || []) {
      if (polygon.length < 3) continue;
      const shape = new THREE.Shape(polygon.map(([x, y]) => new THREE.Vector2(x, y)));
      const area = new THREE.Mesh(new THREE.ShapeGeometry(shape), this.materials.disallowed);
      area.position.z = 0.1;
      this.volume.add(area);
    }

    // Front marker: a small triangle at the front edge (printer -Y).
    const marker = new THREE.Mesh(new THREE.CircleGeometry(4, 3), this.materials.marker);
    marker.rotation.z = -Math.PI / 2;
    marker.position.set(0, -depth / 2 - 8, 0);
    this.volume.add(marker);

    this.buildVolume = { width, depth, height };
    this.resetCamera();
  }

  hasGeometry(key) {
    return this.geometries.has(key);
  }

  addGeometry(key, buffer) {
    if (this.geometries.has(key)) return;
    const { positions } = decodeMesh(buffer);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.computeVertexNormals();
    this.geometries.set(key, geometry);
  }

  // objects: [{ id, key, matrix, fits }]. Models are added (once their geometry is there), moved,
  // recoloured or removed to match the list.
  setObjects(objects) {
    const ids = new Set(objects.map((item) => item.id));
    for (const [id, entry] of this.objects) {
      if (!ids.has(id)) {
        this.scene.remove(entry.mesh);
        entry.mesh.material.dispose();
        this.objects.delete(id);
      }
    }
    for (const item of objects) {
      let entry = this.objects.get(item.id);
      if (!entry) {
        const geometry = this.geometries.get(item.key);
        if (!geometry) continue;
        const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
          roughness: 0.55, metalness: 0.05, side: THREE.DoubleSide,
        }));
        mesh.matrixAutoUpdate = false;
        mesh.userData.id = item.id;
        this.scene.add(mesh);
        entry = { mesh, key: item.key, fits: true };
        this.objects.set(item.id, entry);
      }
      this.placeEntry(entry, item.matrix, item.fits);
    }
    const used = new Set([...this.objects.values()].map((entry) => entry.key));
    for (const [key, geometry] of this.geometries) {
      if (!used.has(key) && !objects.some((item) => item.key === key)) {
        geometry.dispose();
        this.geometries.delete(key);
      }
    }
    this.updateSelection();
  }

  // Immediate feedback while Cura works out the real placement.
  setObjectMatrix(id, matrixList) {
    const entry = this.objects.get(id);
    if (!entry) return;
    this.placeEntry(entry, matrixList, entry.fits);
    this.updateSelection();
  }

  placeEntry(entry, matrixList, fits) {
    entry.mesh.matrix.copy(toMatrix4(matrixList));
    entry.mesh.matrixWorldNeedsUpdate = true;
    entry.fits = fits !== false;
    const palette = currentPalette();
    entry.mesh.material.color.setHex(entry.fits ? palette.model : palette.modelBad);
  }

  // The selected model gets an outline, but only when there is more than one.
  setSelected(id) {
    this.selectedId = id;
    this.updateSelection();
  }

  updateSelection() {
    const entry = this.objects.get(this.selectedId);
    this.selectionBox.visible = !!entry && this.objects.size > 1;
    if (this.selectionBox.visible) {
      entry.mesh.updateMatrixWorld(true);
      this.selectionBox.box.setFromObject(entry.mesh);
    }
    this.render();
  }

  // A tap (not a drag of the camera) on a model selects it.
  listenForTaps() {
    const canvas = this.renderer.domElement;
    let down = null;
    canvas.addEventListener("pointerdown", (event) => { down = { x: event.clientX, y: event.clientY }; });
    canvas.addEventListener("pointerup", (event) => {
      if (!down || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 8) return;
      down = null;
      const id = this.pick(event);
      if (id && this.onSelect) this.onSelect(id);
    });
  }

  pick(event) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const pointer = new THREE.Vector2(
      ((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(pointer, this.camera);
    this.scene.updateMatrixWorld();
    const [hit] = raycaster.intersectObjects([...this.objects.values()].map((entry) => entry.mesh), false);
    return hit ? hit.object.userData.id : null;
  }

  resetCamera() {
    const { width, depth, height } = this.buildVolume || { width: 220, depth: 220, height: 250 };
    // Fit the plate plus a part of the volume height, for any aspect ratio (phones are tall and narrow).
    const target = new THREE.Vector3(0, 0, height * 0.12);
    const radius = 0.5 * Math.hypot(width, depth, height * 0.45);
    const verticalFov = THREE.MathUtils.degToRad(this.camera.fov);
    const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * this.camera.aspect);
    const distance = radius / Math.sin(Math.min(verticalFov, horizontalFov) / 2);
    const direction = new THREE.Vector3(0.25, -1, 0.75).normalize();
    this.camera.position.copy(target).addScaledVector(direction, distance);
    this.controls.target.copy(target);
    this.controls.update();
    this.render();
  }

  resize() {
    const { clientWidth: width, clientHeight: height } = this.container;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    const firstSize = !this.hasSize;
    this.hasSize = true;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    if (firstSize && this.buildVolume) this.resetCamera(); // The first fit happened before the size was known.
    this.render();
  }

  render() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.renderer.render(this.scene, this.camera);
    });
  }

  dispose() {
    window.removeEventListener("rwc-themechange", this.onThemeChange);
    this.resizeObserver.disconnect();
    this.controls.dispose();
    for (const entry of this.objects.values()) entry.mesh.material.dispose();
    for (const geometry of this.geometries.values()) geometry.dispose();
    for (const material of Object.values(this.materials)) material.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

// Rotation of `degrees` around a printer axis ("x", "y", "z") as a row-major list.
export function rotationList(axis, degrees) {
  const angle = THREE.MathUtils.degToRad(degrees);
  const matrix = axis === "x" ? new THREE.Matrix4().makeRotationX(angle)
    : axis === "y" ? new THREE.Matrix4().makeRotationY(angle) : new THREE.Matrix4().makeRotationZ(angle);
  // Exact values for multiples of 90 degrees (no 6.1e-17 noise sent to the API).
  const list = fromMatrix4(matrix).map((v) => Math.round(v * 1e12) / 1e12);
  return list.map((v) => (Object.is(v, -0) ? 0 : v));
}

// Translation as a row-major list.
export function translationList(x, y, z) {
  return [1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1];
}

// left * right, both row-major lists.
export function multiply(left, right) {
  return fromMatrix4(toMatrix4(left).multiply(toMatrix4(right)));
}
