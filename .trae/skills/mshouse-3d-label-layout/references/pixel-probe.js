/*
 * MSHouse 世界标识 · 投影 + GPU 像素取证脚本
 *
 * 用法：在浏览器前台标签（已加载沙盘、相机动画结束）的 console / browser_evaluate
 * 中整段执行。browser_evaluate 通道若拿不到返回值，用 return (function(){...})() 包裹。
 *
 * 把 TARGET 换成目标 sprite 访问路径（默认 siteLabel）。
 * 判据：
 *   - boxCss 与 DOM 遮挡区（顶栏/胶囊/控制面板 getBoundingClientRect）无重叠，留 ≥30px
 *   - light（浅色文字像素）明显 > 0，dark（深底牌框）占比高
 *   - 金边 gold 可能因半透明混合为 0，不作为失败判据
 */
(function () {
  var s = window.__mshouse.scene;
  var cam = s.camera;
  var sp = s.siteLabel.sprite;            // ← 换成目标 sprite
  var worldW = sp.scale.x, worldH = sp.scale.y;

  // 确保拿到的是当前帧
  s.renderer.render(s.scene, cam);
  var gl = s.renderer.getContext();
  var W = s.canvas.width, H = s.canvas.height;
  var dpr = W / window.innerWidth;

  var c = sp.position.clone().project(cam);
  var dist = cam.position.distanceTo(sp.position);
  var halfH = Math.tan((cam.fov * Math.PI) / 360) * dist;
  var hPx = (worldH / (2 * halfH)) * H;
  var wPx = (worldW / (2 * halfH)) * (H * cam.aspect);

  var cx = (c.x * 0.5 + 0.5) * W;
  var cy = (-c.y * 0.5 + 0.5) * H;
  var x0 = Math.round(cx - wPx / 2), x1 = Math.round(cx + wPx / 2);
  var y0 = Math.round(cy - hPx / 2), y1 = Math.round(cy + hPx / 2);

  var buf = new Uint8Array((x1 - x0) * (y1 - y0) * 4);
  gl.readPixels(x0, H - y1, x1 - x0, y1 - y0, gl.RGBA, gl.UNSIGNED_BYTE, buf);

  var light = 0, gold = 0, dark = 0, total = 0;
  for (var i = 0; i < buf.length; i += 4) {
    var r = buf[i], g = buf[i + 1], b = buf[i + 2];
    total++;
    if (r > 210 && g > 200 && b > 170) light++;
    if (r > 170 && g > 120 && b < 130) gold++;
    if (r < 70 && g < 80 && b < 75) dark++;
  }

  // 对照用：打印当前主要 DOM 遮挡区，人工与 boxCss 比对
  var blockers = ["#topbar", ".chip", ".panel"].map(function (sel) {
    var el = document.querySelector(sel);
    if (!el) return null;
    var r = el.getBoundingClientRect();
    return { sel: sel, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
  }).filter(Boolean);

  return {
    ndc: [+c.x.toFixed(3), +c.y.toFixed(3)],
    boxCss: {
      x0: Math.round(x0 / dpr), x1: Math.round(x1 / dpr),
      y0: Math.round(y0 / dpr), y1: Math.round(y1 / dpr),
    },
    counts: { light: light, gold: gold, dark: dark, total: total },
    camera: cam.position.toArray(),
    tween: s.tween ? "running" : "done",
    blockers: blockers,
  };
})();
