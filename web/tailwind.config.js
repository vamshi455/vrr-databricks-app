/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    // ONE type scale for the whole app, named for the job rather than the pixels — a
    // component asks for `text-label`, not `text-[11px]`. The old build drifted into
    // four different "small" sizes on a single screen because every file picked its own.
    //
    // Tightened one notch across the board (display 24→20, title 18→16, sub 15→13.5,
    // body 13→12, label 12→11.5) plus shorter leading. The bottom stops at 11px and does
    // not go below: this app is read as columns of figures for long stretches, and under
    // 11px the glyphs fail regardless of what the contrast ratio says. Most of the
    // visible reduction therefore comes from the headings and the leading, which is
    // where the old build actually looked oversized.
    fontSize: {
      micro: ["0.6875rem", { lineHeight: "0.95rem" }],   // 11px — captions, provenance
      label: ["0.71875rem", { lineHeight: "1.05rem" }],  // 11.5px — form labels, cells
      body: ["0.75rem", { lineHeight: "1.25rem" }],      // 12px — prose, chat, buttons
      sub: ["0.84375rem", { lineHeight: "1.2rem" }],     // 13.5px — card titles
      title: ["1rem", { lineHeight: "1.4rem" }],         // 16px — view headings
      display: ["1.25rem", { lineHeight: "1.7rem" }],    // 20px — metric values
    },
    extend: {
      colors: {
        // ---- Dark surfaces ---------------------------------------------------
        // Four steps only: the page, the cards on it, anything raised above a card
        // (inputs, hovers, table headers), and two rule weights. Card-on-page is 1.22:1
        // — measured, because below about 1.15 the cards stop reading as separate
        // objects and the screen flattens into one undifferentiated sheet.
        surface: {
          base: "#070b11",       // the page
          card: "#18212d",       // cards, header, rail
          raised: "#222d3c",     // inputs, hover, table head
          border: "#2f3b4d",     // a visible hairline, not a wall
          divider: "#232e3d",    // inside a card, quieter still
        },
        // ---- Text ------------------------------------------------------------
        // All three clear 4.5:1 on the card surface: 14.4, 8.1 and 6.0.
        content: {
          primary: "#e8eef5",
          secondary: "#a6b6c8",
          muted: "#8b9db0",
        },
        // ---- Brand -----------------------------------------------------------
        // Blue carries the chrome; crimson is the accent, and it appears ONLY on
        // non-status furniture (the mark, the active-nav bar, section rules). Status red
        // stays brighter and more saturated than the accent, so "off target" can never
        // read as decoration — the one real hazard in a red-and-blue scheme for an app
        // whose whole job is telling you when a number is wrong.
        brand: {
          50: "#0f1a24", 100: "#16283a", 300: "#3d87b8",
          500: "#5aa9dd", 600: "#4a97ca", 700: "#3d87b8", 900: "#cfe4f3",
        },
        accent: { DEFAULT: "#d9576b", soft: "#2a141a", dim: "#a8394a" },
        // ---- Semantic --------------------------------------------------------
        // Re-tuned for a dark ground: the light-theme hues (#2f855a, #b7791f, #c53030)
        // sit at 1.9–2.6:1 here and would be unreadable. These measure 7.6, 7.9 and 6.2.
        signal: { DEFAULT: "#4fc47f", soft: "#10281c" },     // on target / verified
        suspect: { DEFAULT: "#e0a83a", soft: "#2a2210",      // low-confidence inputs
                   text: "#e0a83a" },                        // 7.9:1 — no darker shade needed
        offtarget: { DEFAULT: "#f2777a", soft: "#2b1418" },  // out of band / refused
        // One hue per approval lane, in chain order, so a card's colour says where it is
        // before you read a word of it.
        stage: {
          draft: "#8b9db0", analyst: "#5aa9dd", rm: "#a98bdc",
          site: "#e0a83a", executed: "#4fc47f", rejected: "#f2777a",
        },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", '"Segoe UI"',
               "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      boxShadow: {
        // Shadows do almost nothing on a dark ground — separation comes from the surface
        // step and the hairline border instead. Kept only for a little depth.
        card: "0 1px 2px rgba(0,0,0,0.4)",
        panel: "0 16px 40px rgba(0,0,0,0.55)",
      },
    },
  },
  plugins: [],
};
