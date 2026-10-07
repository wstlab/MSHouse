/**
 * MSHouse 镜像家居 · 网关入口（Mirror Space 智能家居教学场景）
 * HTTP  提供前端静态资源 + 视频流通道（/?stream=<通道名>，MJPEG）
 * WebSocket 提供设备控制、状态与流信令（不含像素）
 */
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";
import { Gateway } from "./gateway.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const PUBLIC = path.join(ROOT, "public");
const SHARED = path.join(ROOT, "shared");   // 物模型前后端共用同一份文件
const PORT = Number(process.env.PORT || 8080);
const HOST = "0.0.0.0";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".md": "text/markdown; charset=utf-8",
};

const gateway = new Gateway();

const server = http.createServer((req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);

  // 视频流通道：GET /?stream=233666 → multipart/x-mixed-replace 长连接
  // 像素走这里，不走 WebSocket；浏览器 <img src> 直接就能播。
  const channel = url.searchParams.get("stream");
  if (channel) {
    gateway.hub.attach(req, res, channel);
    return;
  }
  if (url.pathname === "/streams") {
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
    res.end(JSON.stringify({ streams: gateway.hub.list() }, null, 2));
    return;
  }
  if (url.pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({
      ok: true,
      service: "mshouse",
      clients: gateway.clients.size,
      streams: gateway.hub.list(),
    }));
    return;
  }
  if (url.pathname === "/api/model") {
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify(gateway.snapshot()));
    return;
  }
  let rel = decodeURIComponent(url.pathname);
  if (rel === "/") rel = "/index.html";
  const base = rel.startsWith("/shared/") ? SHARED : PUBLIC;
  const file = path.normalize(path.join(base, rel.startsWith("/shared/") ? rel.slice(8) : rel));
  if (!file.startsWith(base)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  fs.readFile(file, (err, buf) => {
    if (err) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("not found");
      return;
    }
    res.writeHead(200, {
      "Content-Type": MIME[path.extname(file)] || "application/octet-stream",
      "Cache-Control": "no-cache",
    });
    res.end(buf);
  });
});

const wss = new WebSocketServer({ server, path: "/ws" });
wss.on("connection", (ws) => {
  gateway.attach(ws);
  ws.on("message", (data) => gateway.handle(ws, data.toString()));
  ws.on("close", () => gateway.detach(ws));
  ws.on("error", () => gateway.detach(ws));
});

server.listen(PORT, HOST, () => {
  console.log(`mshouse listening on http://${HOST}:${PORT}`);
});
