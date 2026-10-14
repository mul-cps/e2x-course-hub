# Course Service UI

A modern React + TypeScript application built with Vite for the e2x course hub platform.

## Getting Started

This project uses Vite for fast development and building with HMR (Hot Module Replacement) and ESLint for code quality.

## Development

To start the development server:

```bash
npm run dev
```

The application will be available at `http://localhost:5173/`

## Building for Production

```bash
npm run build
```

## Preview Production Build

```bash
npm run preview
```

## Project Structure

- `src/` - Source code
  - `App.tsx` - Main application component
  - `main.tsx` - Application entry point
- `public/` - Static assets
- `index.html` - HTML template

## Technology Stack

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Babel](https://babeljs.io/) (or [oxc](https://oxc.rs) when used in [rolldown-vite](https://vite.dev/guide/rolldown)) for Fast Refresh
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/) for Fast Refresh

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(["dist"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ["./tsconfig.node.json", "./tsconfig.app.json"],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
]);
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from "eslint-plugin-react-x";
import reactDom from "eslint-plugin-react-dom";

export default defineConfig([
  globalIgnores(["dist"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs["recommended-typescript"],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ["./tsconfig.node.json", "./tsconfig.app.json"],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
]);
```


## Current admin data

Compute and shared workspace records, profile choices and authorized references are API-backed. Read-only discovery refreshes every 20 seconds while the page is visible. Each reader has one active request, an eight-second deadline and a generation guard; navigation, changed scope, manual refresh and visibility changes cancel obsolete reads. Returning to a visible page requests current data immediately. Reads keep same-origin credentials and XSRF headers and use `cache: "no-store"`; mutation behavior is unchanged.

Refresh updates record and permission data without replacing a grant/workspace draft, the person/search input, the open dialog or its selected profile. A removed choice stays selected and is labeled unavailable. Saving an unavailable or unverified profile remains blocked; the API always enforces the final policy. An open workspace profile reflects the latest stored state while preserving an in-progress proposed profile. Failed or timed-out reads retain the last records with a stale/error indicator and retry automatically; old readiness is not new authority.

Run `npm run test:ui` for the real Chromium lifecycle, cancellation, stale-response, permission-change, focus, authentication and mutation regressions. `npm run build` verifies TypeScript and the deployable frontend bundle. Passing these checks produces a source candidate; both-console image/deployment qualification remains a separate release step.
