# vendor/ - 폐쇄망 프론트엔드 의존성

인터넷이 되는 환경에서 아래 3개 파일을 받아 이 폴더에 그대로 넣고,
폐쇄망 빌드 서버로 옮기세요. index.html이 이 경로를 그대로 참조합니다.

```bash
curl -L -o react.production.min.js https://unpkg.com/react@18/umd/react.production.min.js
curl -L -o react-dom.production.min.js https://unpkg.com/react-dom@18/umd/react-dom.production.min.js
curl -L -o babel.min.js https://unpkg.com/@babel/standalone@7/babel.min.js
```

`-L` 필수: unpkg.com은 버전 없는 URL을 302로 리다이렉트하는데, `-L` 없이 받으면 리다이렉트
안내문 자체가 파일로 저장된다.

`@babel/standalone`는 **꼭 `@7`로 고정**한다. 버전을 안 박으면 최신 메이저(8.x)가 잡히는데,
8.x는 `transformScriptTags()`가 이 프로젝트의 UMD 전역(React/ReactDOM) + non-module
`<script type="text/babel">` 구성과 안 맞아 `Cannot use import statement outside a module`로 깨진다.
