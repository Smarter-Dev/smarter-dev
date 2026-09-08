import { writeFile } from 'node:fs/promises';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';
import { createMascot,createClips } from './model.js';
// Three's exporter uses FileReader even for geometry-only Node exports.
globalThis.FileReader=class{async readAsArrayBuffer(blob){this.result=await blob.arrayBuffer();this.onloadend?.();} async readAsDataURL(blob){this.result=`data:${blob.type};base64,${Buffer.from(await blob.arrayBuffer()).toString('base64')}`;this.onloadend?.();}};
const root=createMascot(),animations=createClips(root);
const result=await new GLTFExporter().parseAsync(root,{binary:true,animations,onlyVisible:false});
await writeFile(new URL('./public/smarter-orb.glb',import.meta.url),Buffer.from(result));
console.log(`Exported ${result.byteLength} bytes; 8 mood loops and 2 shell transition clips.`);
