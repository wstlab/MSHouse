---
name: mshouse-3d-label-layout
description: 在 mshouse-py 沙盘（public/js/scene.js）中新增跟随世界坐标的 3D 文字标识并避让 DOM 面板，或调整初始机位。用于房名/地点标牌、悬浮标注、世界空间 UI 的摆放与验证。不用于普通 HTML/CSS 弹窗或纯二维 toast。
---

# MSHouse 3D 文字标识摆放与验证

面向 `mshouse-py` 的 THREE.js 沙盘。核心难点不是"把文字放进 3D"，而是**世界坐标的标识会随机位移动，必须与固定的 DOM 面板（顶栏/信息胶囊/控制面板）共存**。

## 项目事实（先看这些，不要重新摸索）

- 标签类 `Label` 在 [public/js/scene.js](file:///Users/xiezuoru/Documents/GitHub/MSHouse/mshouse-py/public/js/scene.js) 顶部：Canvas 纹理 + `THREE.Sprite`，构造参数 `{text, worldHeight, color, font, w, h}`，`setText()` 有同名短路。
- 房间标签先例 `_buildRoomLabels()`：一律 `material.depthTest=false` + `renderOrder=900`，教学沙盘里文字永远压在几何体之上。新增世界标识沿用此约定。
- 建筑常量：`X0=-6,X1=6,Z0=-4.5,Z1=4.5,FH=3.0,GROUND_Y=-0.3,DOOR_X=-3.4`；`Z1` 一侧是房前（入户门、雨棚、台阶）。
- 初始机位在 `_initScene()` 的 `camera.position.set(...)`，controls.target `(0,2.6,0)`；`setFloor()` 会用 `_tweenCamera(target,dist)` 重设机位。
- 数据入口：`applySnapshot(snap)`（含 `snap.site`）与 main.js 的 `handle()`；`window.__mshouse = {scene, ui, state, send}` 是现成的调试钩子。
- **静态资源强缓存**：改 `scene.js/ui.js/main.js/app.css` 后必须同时升版本号，否则浏览器用旧模块，表现为"改动不生效、排查半天"：
  - `public/index.html` 的 `?v=N`（css 与 main.js 两处）
  - `public/js/main.js` 顶部三个 import 的 `?v=N`（protocol/scene/ui）

## 摆放步骤

1. 在 `_buildSite()` 或专门的 build 方法里建 `Label`，加入 `this.root`（不要加楼层组——楼层组会在全屋/分层视图整体挪开）。
2. 先放"语义正确"的世界坐标（如房前檐口 `(0, 6.4, Z1+0.2)`），不要一开始就为屏幕位置乱设坐标。
3. 用浏览器投影试算反推屏幕落点（见 `references/pixel-probe.js`）：世界点 `project(camera)` → NDC → CSS 像素。
4. 对照当前**真实的** DOM 遮挡区量尺寸（让浏览器代理读出 `getBoundingClientRect()`，不要凭感觉）：
   - 顶栏、状态胶囊行（已连接/在家/室外/在线）、右侧控制面板展开时的顶沿。
   - 目标：标识完整矩形落在所有遮挡区之外，留 ≥30px 余量。
5. 屏幕位置不对时，优先**调初始机位**而非把标识挪到语义错误的位置；机位改动会同时影响房子构图，改后必须复查整体观感。
6. 面板展开/收起两种状态都要确认；用户后续旋转/缩放时标识回归真实世界坐标是预期行为，不处理。

## 验证（截图工具不可靠时用数值取证）

浏览器子代理的 `browser_take_screenshot` 经常 IDE 超时；**不要卡在截图上**，按下列顺序取证：

1. **NDC/屏幕坐标**：`sprite.position.clone().project(camera)`，确认 x/y 与遮挡区无交集。
2. **GPU 像素读取**（最可靠）：`renderer.render()` 后按 sprite 世界宽高与透视投影估算屏幕矩形，`gl.readPixels()` 统计文字浅色像素与深底像素占比。浅色文字像素应明显 >0（标牌有字），不能只见框不见字。完整脚本见 `references/pixel-probe.js`。
3. 运行时状态：`window.__mshouse.scene.siteLabel.text` / `sprite.position.toArray()` / `material.opacity`。

## 已知坑（每个都真实踩过）

- **后台标签页 `requestAnimationFrame` 节流/冻结**：相机 tween 看似"不执行"或长时间 `running`，相机停在初始值。让浏览器代理保持目标标签前台、导航后再等待；判定机位前先确认 `tween==='done'` 且相机位置是终态，别拿初始机位做投影结论。
- **版本号未升 = 看的还是旧代码**：代理返回的世界坐标与代码不符时，先查 `index.html`/import 的 `?v=`。
- **信息胶囊恰好压住标识**：NDC y≈0 的屏幕中部不是安全区，胶囊是常驻的。檐口/门头高位通常比地面立牌更容易无遮挡。
- **IIFE 返回值丢失**：部分 `browser_evaluate` 通道拿不到箭头 IIFE 的返回，让代理改用 `return (function(){...})()` 形式。
- 标牌金边是半透明描边，缩放后与深底混合，readPixels 的金色阈值可能为 0；以"浅色文字像素 >0 + 深底占比高"为判据，不要误判成没渲染。

## 改动后收尾

- 升 `?v=` 版本号（两处文件，见上）。
- `GetDiagnostics` 零报错；纯前端改动无需重启 8081。
- 不写业务假数据；验证用的临时浏览器标签关闭即可，不落测试脚本到仓库。
