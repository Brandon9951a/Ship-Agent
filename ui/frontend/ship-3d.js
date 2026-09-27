import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const stage = document.querySelector("#vessel-stage");
const canvas = document.querySelector("#vessel-canvas");
const loading = document.querySelector("#vessel-loading");
const playButton = document.querySelector("#vessel-play");
const rateButton = document.querySelector("#vessel-rate");
const resetViewButton = document.querySelector("#vessel-reset-view");
const segmentSelect = document.querySelector("#vessel-segment-select");
const stateLabel = document.querySelector("#vessel-state");
const stateDot = document.querySelector("#vessel-state-dot");
const routeLabel = document.querySelector("#vessel-route");
const speedLabel = document.querySelector("#vessel-speed");
const socLabel = document.querySelector("#vessel-soc");
const energyLabel = document.querySelector("#vessel-energy");
const etaLabel = document.querySelector("#vessel-eta");

const playback = {
  segments: [],
  summary: {},
  index: 0,
  progress: 0,
  playing: false,
  completed: false,
  rate: 1,
};

let renderer;
let scene;
let camera;
let controls;
let ship;
let wake;
let waterTexture;
let water;
let visible = true;
let previousFrame = performance.now();

function numberValue(field) {
  const value = field && typeof field === "object" && "value" in field ? field.value : field;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatNumber(value, suffix, digits = 1) {
  return value == null ? "未知" : `${value.toFixed(digits)}${suffix}`;
}

function formatEta(value) {
  if (!value) return "未知";
  const text = String(value).replace("T", " ");
  return text.length >= 16 ? text.slice(5, 16) : text;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[character]));
}

function setPlaybackStatus(text, kind = "") {
  stateLabel.textContent = text;
  stateDot.className = `vessel-state-dot${kind ? ` ${kind}` : ""}`;
}

function refreshControlIcon() {
  if (window.lucide) window.lucide.createIcons();
}

function setPlayButton() {
  const restart = playback.completed;
  const icon = playback.playing ? "pause" : restart ? "rotate-ccw" : "play";
  const label = playback.playing ? "暂停回放" : restart ? "重新回放" : "开始回放";
  playButton.innerHTML = `<i data-lucide="${icon}"></i><span>${label}</span>`;
  refreshControlIcon();
}

function clearReadout() {
  routeLabel.textContent = "未知 / 待核实";
  speedLabel.textContent = "未知";
  socLabel.textContent = "未知";
  energyLabel.textContent = "未知";
  etaLabel.textContent = "未知";
}

function segmentPlaybackSeconds(segment) {
  const totalHours = playback.segments.reduce((total, item) => total + (numberValue(item.duration_h) || 0), 0);
  const segmentHours = numberValue(segment.duration_h);
  if (!segmentHours || !totalHours) return Math.max(3, 24 / Math.max(playback.segments.length, 1));
  return Math.max(3, 24 * segmentHours / totalHours);
}

function segmentSocStart(index) {
  if (index > 0) return numberValue(playback.segments[index - 1]?.soc_end);
  return numberValue(playback.summary.soc_initial);
}

function cumulativeEnergy(index, progress) {
  let total = 0;
  for (let position = 0; position < index; position += 1) {
    total += numberValue(playback.segments[position]?.energy_kwh) || 0;
  }
  return total + (numberValue(playback.segments[index]?.energy_kwh) || 0) * progress;
}

function renderPlaybackReadout() {
  const segment = playback.segments[playback.index];
  if (!segment) {
    clearReadout();
    return;
  }
  const startSoc = segmentSocStart(playback.index);
  const endSoc = numberValue(segment.soc_end);
  const currentSoc = startSoc == null || endSoc == null
    ? null
    : startSoc + (endSoc - startSoc) * playback.progress;
  routeLabel.textContent = segment.route || `${segment.origin || "未知"} → ${segment.destination || "未知"}`;
  speedLabel.textContent = formatNumber(numberValue(segment.speed_kmh), " km/h", 2);
  socLabel.textContent = formatNumber(currentSoc == null ? null : currentSoc * 100, "%", 1);
  energyLabel.textContent = formatNumber(cumulativeEnergy(playback.index, playback.progress), " kWh", 1);
  etaLabel.textContent = formatEta(playback.summary.eta?.value ?? playback.summary.eta);
  if (segmentSelect.selectedIndex !== playback.index) segmentSelect.selectedIndex = playback.index;
}

function resetPlayback() {
  playback.index = 0;
  playback.progress = 0;
  playback.completed = false;
}

function stopWithStatus(text, kind = "error") {
  playback.playing = false;
  playback.completed = false;
  playback.segments = [];
  playback.summary = {};
  segmentSelect.innerHTML = "<option>等待仿真方案</option>";
  segmentSelect.disabled = true;
  playButton.disabled = true;
  rateButton.disabled = true;
  clearReadout();
  setPlaybackStatus(text, kind);
  setPlayButton();
}

function updatePlan(segments, summary) {
  playback.segments = Array.isArray(segments) ? segments : [];
  playback.summary = summary || {};
  resetPlayback();
  if (!playback.segments.length) {
    stopWithStatus("无可回放航段", "error");
    return;
  }
  segmentSelect.innerHTML = playback.segments.map((segment, index) => {
    const route = segment.route || `${segment.origin || "未知"} → ${segment.destination || "未知"}`;
    return `<option value="${index}">${index + 1}. ${escapeHtml(route)}</option>`;
  }).join("");
  segmentSelect.disabled = false;
  playButton.disabled = false;
  rateButton.disabled = false;
  playback.playing = true;
  setPlaybackStatus(`回放中 · 航段 1/${playback.segments.length}`, "success");
  renderPlaybackReadout();
  setPlayButton();
}

function handleWorkflowUpdate(event) {
  const detail = event.detail || {};
  if (detail.status === "success") {
    updatePlan(detail.segments, detail.summary);
    return;
  }
  if (detail.status === "running") {
    stopWithStatus(detail.message || "五工具链计算中", "running");
    return;
  }
  if (["infeasible", "incomplete", "failed"].includes(detail.status)) {
    stopWithStatus(detail.message || "当前没有仿真回放", "error");
    return;
  }
  stopWithStatus("等待计算", "");
}

function advancePlayback(deltaSeconds) {
  if (!playback.playing || !playback.segments.length) return;
  const segment = playback.segments[playback.index];
  playback.progress += deltaSeconds * playback.rate / segmentPlaybackSeconds(segment);
  while (playback.progress >= 1) {
    playback.progress -= 1;
    if (playback.index < playback.segments.length - 1) {
      playback.index += 1;
    } else {
      playback.index = playback.segments.length - 1;
      playback.progress = 1;
      playback.playing = false;
      playback.completed = true;
      setPlaybackStatus("回放完成", "success");
      setPlayButton();
      break;
    }
  }
  if (playback.playing) {
    setPlaybackStatus(`回放中 · 航段 ${playback.index + 1}/${playback.segments.length}`, "success");
  }
  renderPlaybackReadout();
}

function createWaterTexture() {
  const textureCanvas = document.createElement("canvas");
  textureCanvas.width = 512;
  textureCanvas.height = 512;
  const context = textureCanvas.getContext("2d");
  context.fillStyle = "#285d6d";
  context.fillRect(0, 0, 512, 512);
  for (let index = 0; index < 520; index += 1) {
    const x = (index * 73) % 512;
    const y = (index * 191) % 512;
    const width = 5 + (index * 17) % 31;
    context.fillStyle = index % 3 === 0
      ? "rgba(185, 218, 224, .035)"
      : "rgba(8, 44, 57, .028)";
    context.fillRect(x, y, width, 1);
  }
  for (let row = 5; row < 512; row += 11) {
    context.strokeStyle = `rgba(213, 235, 239, ${0.045 + (row % 37) / 430})`;
    context.lineWidth = row % 4 === 0 ? 1.4 : 0.7;
    context.beginPath();
    for (let x = 0; x <= 512; x += 6) {
      const y = row + Math.sin(x * 0.046 + row * 0.11) * 3.5
        + Math.sin(x * 0.13 - row * 0.035) * 1.4;
      if (x === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    }
    context.stroke();
  }
  const texture = new THREE.CanvasTexture(textureCanvas);
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(4.5, 24);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8);
  return texture;
}

function seededRandom(seed = 42871) {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

function shorelineAt(z, side) {
  return 34 + Math.sin(z * 0.021 + side * 0.8) * 4.1
    + Math.sin(z * 0.047 - side * 1.7) * 1.8;
}

function terrainHeight(t, z, side) {
  if (t <= 0) return -0.25;
  const cliff = 43 * Math.pow(t, 0.58);
  const folds = Math.sin(z * 0.033 + t * 7.2 + side) * (2.2 + t * 2.7)
    + Math.sin(z * 0.081 - t * 4.4) * 1.4;
  return Math.max(0, cliff + folds - 1.8);
}

function createSkyTexture() {
  const skyCanvas = document.createElement("canvas");
  skyCanvas.width = 32;
  skyCanvas.height = 512;
  const context = skyCanvas.getContext("2d");
  const gradient = context.createLinearGradient(0, 0, 0, 512);
  gradient.addColorStop(0, "#87a9c4");
  gradient.addColorStop(0.42, "#b7cddd");
  gradient.addColorStop(0.72, "#dce5e7");
  gradient.addColorStop(1, "#98adb2");
  context.fillStyle = gradient;
  context.fillRect(0, 0, 32, 512);
  const texture = new THREE.CanvasTexture(skyCanvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function createCanyonSide(side) {
  const longitudinal = 72;
  const cross = 10;
  const positions = [];
  const colors = [];
  const indices = [];
  const greenDark = new THREE.Color(0x274735);
  const greenLight = new THREE.Color(0x527553);
  const rock = new THREE.Color(0x6f766d);
  const color = new THREE.Color();

  for (let row = 0; row <= longitudinal; row += 1) {
    const z = -330 + row / longitudinal * 500;
    const shore = shorelineAt(z, side);
    for (let column = 0; column <= cross; column += 1) {
      const t = column / cross;
      const outward = shore + t * (111 - shore) + Math.sin(row * 0.37 + column) * t * 2.4;
      positions.push(side * outward, terrainHeight(t, z, side), z);
      if (t < 0.13) color.copy(rock).lerp(greenDark, t / 0.13 * 0.35);
      else color.copy(greenDark).lerp(greenLight, Math.min(1, t * 0.82 + (row % 7) * 0.035));
      colors.push(color.r, color.g, color.b);
    }
  }
  for (let row = 0; row < longitudinal; row += 1) {
    for (let column = 0; column < cross; column += 1) {
      const a = row * (cross + 1) + column;
      const b = a + cross + 1;
      indices.push(a, b, a + 1, b, b + 1, a + 1);
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  const terrain = new THREE.Mesh(
    geometry,
    new THREE.MeshStandardMaterial({
      vertexColors: true,
      roughness: 0.98,
      metalness: 0,
      flatShading: true,
      side: THREE.DoubleSide,
    }),
  );
  terrain.receiveShadow = true;
  terrain.castShadow = true;
  scene.add(terrain);
}

function addShoreRocks() {
  const random = seededRandom(90210);
  const geometry = new THREE.DodecahedronGeometry(1, 0);
  const material = new THREE.MeshStandardMaterial({ color: 0x788078, roughness: 1, flatShading: true });
  const rocks = new THREE.InstancedMesh(geometry, material, 260);
  const matrix = new THREE.Matrix4();
  const rotation = new THREE.Quaternion();
  const scale = new THREE.Vector3();
  const position = new THREE.Vector3();
  for (let index = 0; index < 260; index += 1) {
    const side = index % 2 === 0 ? -1 : 1;
    const z = -320 + random() * 485;
    const size = 0.45 + random() * 1.55;
    position.set(side * (shorelineAt(z, side) + random() * 3.2), -0.05 + size * 0.38, z);
    rotation.setFromEuler(new THREE.Euler(random() * 2, random() * 2, random() * 2));
    scale.set(size * (0.8 + random() * 0.7), size * (0.45 + random() * 0.45), size);
    matrix.compose(position, rotation, scale);
    rocks.setMatrixAt(index, matrix);
  }
  rocks.castShadow = true;
  rocks.receiveShadow = true;
  scene.add(rocks);
}

function addForest() {
  const random = seededRandom(33119);
  const crownGeometry = new THREE.IcosahedronGeometry(1, 1);
  const crownMaterial = new THREE.MeshStandardMaterial({ color: 0x315b38, roughness: 1, flatShading: true });
  const count = 980;
  const forest = new THREE.InstancedMesh(crownGeometry, crownMaterial, count);
  const matrix = new THREE.Matrix4();
  const color = new THREE.Color();
  for (let index = 0; index < count; index += 1) {
    const side = index % 2 === 0 ? -1 : 1;
    const z = -320 + random() * 490;
    const t = 0.055 + Math.pow(random(), 1.12) * 0.925;
    const shore = shorelineAt(z, side);
    const x = side * (shore + 1.4 + t * (106 - shore));
    const height = terrainHeight(t, z, side);
    const treeScale = 0.8 + random() * 2.15;
    matrix.makeScale(
      treeScale * (0.82 + random() * 0.62),
      treeScale * (0.78 + random() * 0.62),
      treeScale * (0.86 + random() * 0.58),
    );
    matrix.setPosition(x, height + treeScale * 0.82, z);
    forest.setMatrixAt(index, matrix);
    color.setHSL(0.285 + random() * 0.075, 0.31 + random() * 0.27, 0.19 + random() * 0.19);
    forest.setColorAt(index, color);
  }
  forest.instanceMatrix.needsUpdate = true;
  if (forest.instanceColor) forest.instanceColor.needsUpdate = true;
  forest.castShadow = true;
  forest.receiveShadow = true;
  scene.add(forest);

  const coniferCount = 150;
  const conifers = new THREE.InstancedMesh(
    new THREE.ConeGeometry(0.68, 3.7, 7),
    new THREE.MeshStandardMaterial({ color: 0x234a32, roughness: 1, flatShading: true }),
    coniferCount,
  );
  for (let index = 0; index < coniferCount; index += 1) {
    const side = index % 2 === 0 ? -1 : 1;
    const z = -315 + random() * 475;
    const t = 0.14 + random() * 0.84;
    const shore = shorelineAt(z, side);
    const treeScale = 0.8 + random() * 1.55;
    matrix.makeScale(treeScale, treeScale * (0.9 + random() * 0.35), treeScale);
    matrix.setPosition(
      side * (shore + 3 + t * (104 - shore)),
      terrainHeight(t, z, side) + 1.82 * treeScale,
      z,
    );
    conifers.setMatrixAt(index, matrix);
  }
  conifers.instanceMatrix.needsUpdate = true;
  conifers.castShadow = true;
  conifers.receiveShadow = true;
  scene.add(conifers);
}

function addDistantMountains() {
  const geometry = new THREE.ConeGeometry(1, 1, 7, 3);
  const materials = [
    new THREE.MeshStandardMaterial({ color: 0x617985, roughness: 1, flatShading: true }),
    new THREE.MeshStandardMaterial({ color: 0x4e6972, roughness: 1, flatShading: true }),
  ];
  const peaks = [
    [-128, 64, -365], [-88, 82, -385], [-42, 58, -370], [0, 91, -405],
    [47, 69, -378], [90, 88, -402], [136, 66, -370],
  ];
  peaks.forEach(([x, height, z], index) => {
    const mountain = new THREE.Mesh(geometry, materials[index % 2]);
    mountain.scale.set(height * 0.82, height, height * 0.72);
    mountain.position.set(x, height * 0.42 - 3, z);
    mountain.rotation.y = index * 0.46;
    scene.add(mountain);
  });
}

function addHillsideRoad() {
  const points = [];
  for (let index = 0; index <= 22; index += 1) {
    const z = 145 - index * 20;
    const shore = shorelineAt(z, 1);
    const t = 0.43;
    points.push(new THREE.Vector3(shore + t * (106 - shore), terrainHeight(t, z, 1) + 0.8, z));
  }
  const curve = new THREE.CatmullRomCurve3(points);
  const asphalt = new THREE.Mesh(
    new THREE.TubeGeometry(curve, 170, 0.72, 5, false),
    new THREE.MeshStandardMaterial({ color: 0x626967, roughness: 0.94 }),
  );
  asphalt.castShadow = true;
  scene.add(asphalt);
  const railPoints = points.map(point => point.clone().add(new THREE.Vector3(-1.0, 0.75, 0)));
  const rail = new THREE.Mesh(
    new THREE.TubeGeometry(new THREE.CatmullRomCurve3(railPoints), 170, 0.09, 5, false),
    new THREE.MeshStandardMaterial({ color: 0xc7cbc5, roughness: 0.55, metalness: 0.22 }),
  );
  scene.add(rail);
}

function addDistantLife() {
  const random = seededRandom(7153);
  const village = new THREE.Group();
  for (let index = 0; index < 22; index += 1) {
    const building = new THREE.Mesh(
      new THREE.BoxGeometry(2.2 + random() * 2.3, 1.6 + random() * 2.4, 2 + random() * 2.4),
      new THREE.MeshStandardMaterial({ color: index % 4 === 0 ? 0xc4b39a : 0xd7d9d1, roughness: 0.92 }),
    );
    building.position.set(-25 + random() * 50, 1.2, -288 + random() * 18);
    village.add(building);
  }
  scene.add(village);

  [[-7, -92], [8, -168], [-4, -224]].forEach(([x, z], index) => {
    const barge = new THREE.Group();
    const hull = new THREE.Mesh(
      new THREE.BoxGeometry(5.2 - index * 0.55, 1.05, 12 - index * 1.1),
      new THREE.MeshStandardMaterial({ color: 0x354449, roughness: 0.76 }),
    );
    hull.position.y = 0.45;
    const cargo = new THREE.Mesh(
      new THREE.BoxGeometry(4.2 - index * 0.45, 1.3, 6.5 - index * 0.55),
      new THREE.MeshStandardMaterial({ color: 0x2d3a36, roughness: 0.9 }),
    );
    cargo.position.set(0, 1.5, 0.6);
    barge.add(hull, cargo);
    barge.position.set(x, 0, z);
    scene.add(barge);
  });
}

function addEnvironment() {
  scene.background = createSkyTexture();
  scene.fog = new THREE.FogExp2(0x9db4bb, 0.0034);
  scene.add(new THREE.HemisphereLight(0xe4eef2, 0x243d32, 1.12));
  const sun = new THREE.DirectionalLight(0xfff3dd, 2.05);
  sun.position.set(-62, 110, 58);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.camera.left = -115;
  sun.shadow.camera.right = 115;
  sun.shadow.camera.top = 105;
  sun.shadow.camera.bottom = -75;
  sun.shadow.camera.near = 20;
  sun.shadow.camera.far = 330;
  sun.shadow.bias = -0.00025;
  scene.add(sun);

  waterTexture = createWaterTexture();
  water = new THREE.Mesh(
    new THREE.PlaneGeometry(78, 650, 28, 160),
    new THREE.MeshPhysicalMaterial({
      color: 0x2a6474,
      map: waterTexture,
      bumpMap: waterTexture,
      bumpScale: 0.11,
      roughness: 0.34,
      metalness: 0.03,
      clearcoat: 0.58,
      clearcoatRoughness: 0.3,
      envMapIntensity: 1.12,
    }),
  );
  water.rotation.x = -Math.PI / 2;
  water.position.set(0, -0.18, -80);
  water.receiveShadow = false;
  scene.add(water);

  createCanyonSide(-1);
  createCanyonSide(1);
  addShoreRocks();
  addForest();
  addDistantMountains();
  addHillsideRoad();
  addDistantLife();

  const wakeGeometry = new THREE.BufferGeometry();
  wakeGeometry.setAttribute("position", new THREE.Float32BufferAttribute([
    -2, 0, 9, 2, 0, 9, 8, 0, 43,
    -2, 0, 9, 8, 0, 43, -8, 0, 43,
  ], 3));
  wakeGeometry.setAttribute("uv", new THREE.Float32BufferAttribute([
    0.4, 0, 0.6, 0, 1, 1,
    0.4, 0, 1, 1, 0, 1,
  ], 2));
  wakeGeometry.computeVertexNormals();
  wake = new THREE.Mesh(
    wakeGeometry,
    new THREE.MeshBasicMaterial({ color: 0xe7f6fb, transparent: true, opacity: 0.38, depthWrite: false, side: THREE.DoubleSide }),
  );
  wake.position.y = 0.04;
  scene.add(wake);
}

function fitShipModel(model) {
  model.rotation.y = -Math.PI / 2;
  model.updateMatrixWorld(true);
  const initialBox = new THREE.Box3().setFromObject(model);
  const initialSize = initialBox.getSize(new THREE.Vector3());
  const length = Math.max(initialSize.x, initialSize.z, 0.001);
  model.scale.setScalar(28 / length);
  model.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(model);
  const center = box.getCenter(new THREE.Vector3());
  model.position.x -= center.x;
  model.position.z -= center.z;
  model.position.y -= box.min.y - 0.15;
  model.traverse(child => {
    if (child.isMesh) {
      child.castShadow = true;
      child.receiveShadow = true;
      if (child.material?.map) child.material.map.colorSpace = THREE.SRGBColorSpace;
    }
  });
}

function loadShip() {
  const loader = new GLTFLoader();
  loader.load(
    "/assets/ship.glb",
    gltf => {
      fitShipModel(gltf.scene);
      ship.add(gltf.scene);
      loading.hidden = true;
      window.Ship3DReady = true;
    },
    progress => {
      if (!progress.total) return;
      loading.lastChild.textContent = `正在加载船舶模型 ${Math.round(progress.loaded / progress.total * 100)}%`;
    },
    () => {
      loading.classList.add("error");
      loading.lastChild.textContent = "船舶模型加载失败，请检查本地资源";
    },
  );
}

function resetView() {
  camera.position.set(34, 21, 48);
  controls.target.set(0, 3.8, -8);
  controls.update();
}

function handleCanvasKeydown(event) {
  if (!controls) return;
  if (event.key === "Home") {
    resetView();
    event.preventDefault();
    return;
  }
  const offset = camera.position.clone().sub(controls.target);
  const spherical = new THREE.Spherical().setFromVector3(offset);
  const step = Math.PI / 36;
  if (event.key === "ArrowLeft") spherical.theta += step;
  else if (event.key === "ArrowRight") spherical.theta -= step;
  else if (event.key === "ArrowUp") spherical.phi -= step;
  else if (event.key === "ArrowDown") spherical.phi += step;
  else if (event.key === "+" || event.key === "=") spherical.radius *= 0.9;
  else if (event.key === "-") spherical.radius /= 0.9;
  else return;
  spherical.radius = THREE.MathUtils.clamp(spherical.radius, controls.minDistance, controls.maxDistance);
  spherical.makeSafe();
  camera.position.copy(controls.target).add(new THREE.Vector3().setFromSpherical(spherical));
  controls.update();
  event.preventDefault();
}

function resizeRenderer() {
  const width = Math.max(stage.clientWidth, 1);
  const height = Math.max(stage.clientHeight, 1);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

function initializeScene() {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.96;
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  scene = new THREE.Scene();
  const environment = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04);
  scene.environment = environment.texture;
  camera = new THREE.PerspectiveCamera(46, 1, 0.1, 900);
  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.06;
  controls.minDistance = 18;
  controls.maxDistance = 145;
  controls.maxPolarAngle = Math.PI * 0.475;
  controls.enablePan = false;
  resetView();

  ship = new THREE.Group();
  scene.add(ship);
  addEnvironment();
  loadShip();
  resizeRenderer();
}

function animate(now) {
  const deltaSeconds = Math.min((now - previousFrame) / 1000, 0.1);
  previousFrame = now;
  advancePlayback(deltaSeconds);
  if (visible) {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const active = playback.playing && !reducedMotion;
    const time = now / 1000;
    ship.position.y = active ? Math.sin(time * 1.15) * 0.11 : 0;
    ship.rotation.z = active ? Math.sin(time * 0.72) * 0.012 : 0;
    wake.visible = playback.playing;
    wake.material.opacity = playback.playing ? 0.32 + Math.sin(time * 1.8) * 0.06 : 0;
    waterTexture.offset.y -= deltaSeconds * (playback.playing ? 0.024 * playback.rate : 0.0045);
    waterTexture.offset.x += deltaSeconds * 0.0018;
    if (water) water.position.y = -0.18 + Math.sin(time * 0.42) * 0.025;
    controls.update();
    renderer.render(scene, camera);
  }
  requestAnimationFrame(animate);
}

playButton.addEventListener("click", () => {
  if (!playback.segments.length) return;
  if (playback.completed) resetPlayback();
  playback.playing = !playback.playing;
  setPlaybackStatus(
    playback.playing ? `回放中 · 航段 ${playback.index + 1}/${playback.segments.length}` : "回放已暂停",
    "success",
  );
  renderPlaybackReadout();
  setPlayButton();
});

rateButton.addEventListener("click", () => {
  playback.rate = playback.rate === 1 ? 5 : 1;
  rateButton.textContent = `${playback.rate}×`;
});

segmentSelect.addEventListener("change", () => {
  playback.index = Number(segmentSelect.value) || 0;
  playback.progress = 0;
  playback.playing = false;
  playback.completed = false;
  setPlaybackStatus(`已定位航段 ${playback.index + 1}/${playback.segments.length}`, "success");
  renderPlaybackReadout();
  setPlayButton();
});

resetViewButton.addEventListener("click", resetView);
canvas.addEventListener("keydown", handleCanvasKeydown);
document.addEventListener("ship3d:update", handleWorkflowUpdate);
new ResizeObserver(resizeRenderer).observe(stage);
new IntersectionObserver(entries => { visible = entries[0]?.isIntersecting ?? true; }, { rootMargin: "120px" }).observe(stage);

try {
  initializeScene();
  stopWithStatus("等待计算", "");
  if (window.ship3dPendingUpdate) {
    handleWorkflowUpdate({ detail: window.ship3dPendingUpdate });
  }
  requestAnimationFrame(animate);
} catch (_error) {
  loading.classList.add("error");
  loading.lastChild.textContent = "当前浏览器无法初始化三维视图";
}
