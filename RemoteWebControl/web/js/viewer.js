// 3D preview: build volume + model. Printer coordinates everywhere (Z up, mm, origin at the
// centre of the build plate), exactly like the API, so job matrices are applied as they are.

import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";

const COLOR_OK = 0xff8a26;
const COLOR_BAD = 0xff5d5d;

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
    this.renderer.setClearColor(0x11141a);
    container.appendChild(this.renderer.domElement);

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
    this.mesh = null;

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
  }

  setPrinter(printer) {
    this.volume.clear();
    const { x: width, y: depth, z: height } = printer.build_volume;
    const elliptic = printer.shape === "elliptic";

    const plateShape = new THREE.Shape();
    if (elliptic) plateShape.absellipse(0, 0, width / 2, depth / 2, 0, Math.PI * 2);
    else plateShape.moveTo(-width / 2, -depth / 2).lineTo(width / 2, -depth / 2).lineTo(width / 2, depth / 2).lineTo(-width / 2, depth / 2);
    const plate = new THREE.Mesh(new THREE.ShapeGeometry(plateShape, 48),
      new THREE.MeshBasicMaterial({ color: 0x2b3242 }));
    plate.position.z = -0.2;
    this.volume.add(plate);

    // Grid every 10 mm, clipped to the plate for rectangular printers.
    if (!elliptic) {
      const points = [];
      for (let x = -width / 2; x <= width / 2 + 0.01; x += 10) points.push(x, -depth / 2, 0, x, depth / 2, 0);
      for (let y = -depth / 2; y <= depth / 2 + 0.01; y += 10) points.push(-width / 2, y, 0, width / 2, y, 0);
      const grid = new THREE.BufferGeometry();
      grid.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
      this.volume.add(new THREE.LineSegments(grid, new THREE.LineBasicMaterial({ color: 0x394155 })));
    }

    const box = new THREE.BoxGeometry(width, depth, height);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(box), new THREE.LineBasicMaterial({ color: 0x6b7489 }));
    edges.position.z = height / 2;
    if (!elliptic) this.volume.add(edges);

    for (const polygon of printer.disallowed_areas || []) {
      if (polygon.length < 3) continue;
      const shape = new THREE.Shape(polygon.map(([x, y]) => new THREE.Vector2(x, y)));
      const area = new THREE.Mesh(new THREE.ShapeGeometry(shape),
        new THREE.MeshBasicMaterial({ color: 0x7a2f35, transparent: true, opacity: 0.7 }));
      area.position.z = 0.1;
      this.volume.add(area);
    }

    // Front marker: a small triangle at the front edge (printer -Y).
    const marker = new THREE.Mesh(new THREE.CircleGeometry(4, 3),
      new THREE.MeshBasicMaterial({ color: 0x9aa3b5 }));
    marker.rotation.z = -Math.PI / 2;
    marker.position.set(0, -depth / 2 - 8, 0);
    this.volume.add(marker);

    this.buildVolume = { width, depth, height };
    this.resetCamera();
  }

  setMesh(buffer) {
    const { positions } = decodeMesh(buffer);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.computeVertexNormals();
    if (this.mesh) {
      this.scene.remove(this.mesh);
      this.mesh.geometry.dispose();
    }
    this.mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
      color: COLOR_OK, roughness: 0.55, metalness: 0.05, side: THREE.DoubleSide,
    }));
    this.mesh.matrixAutoUpdate = false;
    this.scene.add(this.mesh);
    this.render();
  }

  setPlacement(matrixList, fits) {
    if (!this.mesh) return;
    this.mesh.matrix.copy(toMatrix4(matrixList));
    this.mesh.matrixWorldNeedsUpdate = true;
    this.mesh.material.color.setHex(fits === false ? COLOR_BAD : COLOR_OK);
    this.render();
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
    this.resizeObserver.disconnect();
    this.controls.dispose();
    if (this.mesh) this.mesh.geometry.dispose();
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

// left * right, both row-major lists.
export function multiply(left, right) {
  return fromMatrix4(toMatrix4(left).multiply(toMatrix4(right)));
}
