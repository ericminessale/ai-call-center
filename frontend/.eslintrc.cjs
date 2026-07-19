module.exports = {
  root: true,
  env: {
    browser: true,
    es2022: true,
  },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint', 'react-hooks', 'react-refresh'],
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  ignorePatterns: ['dist', 'node_modules'],
  rules: {
    // The TypeScript compiler already provides stricter unused checks.
    '@typescript-eslint/no-unused-vars': 'off',
    // Existing API and SignalWire payloads intentionally cross untyped
    // boundaries. Tighten these incrementally as those contracts get schemas.
    '@typescript-eslint/no-explicit-any': 'off',
    // Several stores and configuration modules export React components plus
    // helpers. Splitting them is worthwhile, but not a correctness gate.
    'react-refresh/only-export-components': 'off',
  },
};
