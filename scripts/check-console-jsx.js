// 관리자 콘솔의 JSX 문법 검사.
//
// 왜 있나: index.html은 **브라우저에서** Babel standalone으로 트랜스파일된다
// (`<script type="text/babel">`). 그래서 문법 오류가 나면 빌드가 실패하는 게 아니라
// **콘솔이 빈 화면으로 뜬다.** 서버에 올리고 브라우저를 열기 전까지 아무도 모른다.
// 여기서 브라우저와 **같은 트랜스파일러**로 미리 돌려 본다.
//
// babel을 어디서 찾나(순서대로):
//   1) admin_console/frontend/vendor/babel.min.js — 폐쇄망용으로 받아 둔 그 파일 그대로.
//      UMD 빌드라 node의 require로도 읽힌다. 콘솔이 동작하는 환경이면 이게 반드시 있다.
//   2) node_modules의 @babel/standalone — 인터넷이 되는 개발 환경.
// 둘 다 없으면 **건너뛴다**(종료코드 0). 검사 하나 없다고 커밋을 막지는 않는다.
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const target = path.join(root, "admin_console", "frontend", "index.html");

let babel = null;
for (const src of [path.join(root, "admin_console/frontend/vendor/babel.min.js"),
                   "@babel/standalone"]) {
  try {
    babel = require(src);
    if (babel && babel.transform) break;
    babel = null;
  } catch { /* 다음 후보 */ }
}
if (!babel) {
  console.log("(건너뜀 — babel을 찾지 못했습니다. vendor/babel.min.js 또는 npm i @babel/standalone)");
  process.exit(0);
}

const html = fs.readFileSync(target, "utf8");
const blocks = [...html.matchAll(/<script type="text\/babel"[^>]*>([\s\S]*?)<\/script>/g)];
if (blocks.length === 0) {
  console.error("!! text/babel 블록을 찾지 못했습니다 — index.html 구조가 바뀌었습니다.");
  process.exit(1);
}

let lines = 0;
for (const [, code] of blocks) {
  try {
    babel.transform(code, { presets: ["react"], filename: "console.jsx" });
    lines += code.split("\n").length;
  } catch (e) {
    console.error("!! JSX 문법 오류 — 이대로 올리면 콘솔이 빈 화면이 됩니다.");
    console.error("   " + e.message);
    process.exit(1);
  }
}
console.log(`(통과 — ${blocks.length}블록 ${lines}줄)`);
