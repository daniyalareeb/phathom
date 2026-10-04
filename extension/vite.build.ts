import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const root = dirname(fileURLToPath(import.meta.url));

// Multi-entry MV3 build: every entry becomes one top-level file in dist/
// (content scripts / workers cannot be code-split, so each gets its own
// self-contained bundle; UI pages share chunks via manualChunks off).
export default defineConfig({
  plugins: [react()],
  publicDir: 'public',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
    minify: 'esbuild',
    rollupOptions: {
      input: {
        inject: resolve(root, 'src/inject/inject.ts'),
        content: resolve(root, 'src/content/content.ts'),
        background: resolve(root, 'src/background/background.ts'),
        offscreen: resolve(root, 'src/offscreen/offscreen.ts'),
        popup: resolve(root, 'popup.html'),
        sidepanel: resolve(root, 'sidepanel.html'),
        app: resolve(root, 'app.html'),
        preview: resolve(root, 'preview.html'),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
});
