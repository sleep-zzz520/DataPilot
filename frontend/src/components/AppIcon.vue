<template>
  <svg
    viewBox="0 0 24 24"
    :width="size"
    :height="size"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path
      v-for="(path, index) in paths[name] || paths.chat"
      :key="index"
      :d="path"
    />
  </svg>
</template>
<script setup>
defineProps({ name: String, size: { type: [Number, String], default: 18 } });
const paths = {
  brand: ["M12 3 2 8l10 5 10-5-10-5Z", "m2 12 10 5 10-5", "m2 17 10 5 10-5"],
  plus: ["M12 5v14M5 12h14"],
  search: ["M21 21l-5-5", "M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0"],
  chat: ["M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"],
  folder: [
    "M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z",
  ],
  "folder-open": [
    "M3 10V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2",
    "M3 10h19l-3 10H2l1-10Z",
  ],
  chevron: ["m9 5 7 7-7 7"],
  panel: [
    "M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1ZM9 3v18",
  ],
  user: [
    "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
    "M4 21v-2a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2",
  ],
  users: [
    "M15 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
    "M2 21v-2a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2",
    "M19 4a4 4 0 0 1 0 7m0 4a5 5 0 0 1 3 4v2",
  ],
  settings: [
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8",
    "m10 2-.5 3-2 .9L5 4.5 2.5 9l2.5 2v2L2.5 15 5 19.5l2.5-1.4 2 .9.5 3h4l.5-3 2-.9 2.5 1.4 2.5-4.5-2.5-2v-2l2.5-2L19 4.5l-2.5 1.4-2-.9L14 2Z",
  ],
  logout: ["M9 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h4", "M9 12h12m-5-5 5 5-5 5"],
  more: ["M5 12h.01M12 12h.01M19 12h.01"],
  attach: [
    "m21 11-9 9a6 6 0 0 1-8-8L14 2a4 4 0 0 1 6 6L10 18a2 2 0 0 1-3-3l9-9",
  ],
  arrow: ["M12 19V5m-6 6 6-6 6 6"],
  back: ["M20 12H4m7-7-7 7 7 7"],
  close: ["m6 6 12 12M6 18 18 6"],
  expand: ["M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"],
  database: [
    "M21 5c0 2-4 3-9 3S3 7 3 5s4-3 9-3 9 1 9 3Z",
    "M3 5v14c0 2 4 3 9 3s9-1 9-3V5M3 12c0 2 4 3 9 3s9-1 9-3",
  ],
  file: [
    "M14 2H5a1 1 0 0 0-1 1v18a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V8ZM14 2v6h6M8 13h8M8 17h6",
  ],
  circle: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0"],
  loader: ["M21 12a9 9 0 1 1-9-9"],
  "check-circle": ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0", "m8 12 3 3 5-6"],
  "x-circle": ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0", "m9 9 6 6m-6 0 6-6"],
  "minus-circle": ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0", "M8 12h8"],
  copy: ["M9 9h12v12H9ZM5 15H3V3h12v2"],
};
</script>
