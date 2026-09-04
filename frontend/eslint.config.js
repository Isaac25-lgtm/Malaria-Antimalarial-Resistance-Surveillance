import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    // The generated API types are the contract, not hand-written source.
    // "dist-stuck-*" is build output that Windows would not let Vite delete:
    // the build renames the locked directory aside and carries on. Those
    // directories hold minified bundles, so linting them reports thousands of
    // meaningless errors about single-letter variables.
    ignores: ["dist", "dist-verify", "dist-stuck-*", "node_modules", "src/api/schema.d.ts", "tests/e2e", "playwright.config.ts", "playwright-report", "test-results"],
  },

  js.configs.recommended,

  // Type-aware linting is scoped to TypeScript sources. Applying it to this
  // config file itself would demand type information for a plain .js module.
  ...tseslint.configs.recommendedTypeChecked.map((config) => ({
    ...config,
    files: ["**/*.{ts,tsx}"],
  })),

  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
      globals: {
        window: "readonly",
        document: "readonly",
        console: "readonly",
        fetch: "readonly",
        Response: "readonly",
        URLSearchParams: "readonly",
        AbortSignal: "readonly",
        HTMLElement: "readonly",
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
      "jsx-a11y": jsxA11y,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // A floating promise in an event handler swallows its failure silently.
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/consistent-type-imports": [
        "error",
        { prefer: "type-imports", fixStyle: "inline-type-imports" },
      ],
    },
  },

  {
    files: ["tests/**/*.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-call": "off",
    },
  },

  {
    // Config files are plain modules; no type-aware rules apply.
    files: ["*.js", "*.config.js"],
    ...tseslint.configs.disableTypeChecked,
  },
);
