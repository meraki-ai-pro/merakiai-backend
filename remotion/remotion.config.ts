import { Config } from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
// Containers have no usable /dev/shm and no GPU. Without this Chromium either
// crashes or falls back silently to a renderer that produces blank frames.
Config.setChromiumOpenGlRenderer('swiftshader');
