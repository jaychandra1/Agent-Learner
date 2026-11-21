// Where the page sends questions. `npm run build` writes this file into dist/.
// Empty = the page's own address: the host passes questions on to the backend (api/chat.js
// on Vercel), so the backend's address is not in the page at all.
window.PQ_CONFIG = { BACKEND_URL: "" };
