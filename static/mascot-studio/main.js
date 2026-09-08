import * as T from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { SavePass } from 'three/addons/postprocessing/SavePass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { createMascot,createClips,setShellState,moods,descriptions,palette,orbRadii } from './model.js';
const $=id=>document.getElementById(id), host=$('viewport');
const scene=new T.Scene(),camera=new T.PerspectiveCamera(34,1,.1,100);camera.position.set(2,1.05,6.8);
let renderer;
try{renderer=new T.WebGLRenderer({antialias:true,alpha:true,preserveDrawingBuffer:true});}catch(e){$('loading').textContent='This studio needs a browser with WebGL enabled.';throw e;}
renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.toneMapping=T.ACESFilmicToneMapping;renderer.toneMappingExposure=.85;host.append(renderer.domElement);
const pmrem=new T.PMREMGenerator(renderer),room=new RoomEnvironment();scene.environment=pmrem.fromScene(room,.04).texture;room.dispose();pmrem.dispose();
scene.environmentIntensity=.22;
scene.add(new T.HemisphereLight(0xdde5ea,0x101216,.3));
for(const [color,power,pos] of [[0xe8f0ff,12,[3,5,4]],[0xe0eaff,14,[-3,1,-2]],[0xffffff,12,[2,3,-2]]]){const l=new T.PointLight(color,power);l.position.set(...pos);scene.add(l);}
const model=createMascot(),clips=createClips(model);scene.add(model);
// A quiet two-softbox reflection setup for the casing only. The general room
// map includes a bright floor strip that looks like a solid ring in the glass.
const casingRoom=new T.Scene();casingRoom.background=new T.Color(0x020405);
for(const [position,width,height,power] of [[[4,6,-3],5,4,48],[[-4,2,3],2,4,3]]){
  const panel=new T.Mesh(new T.PlaneGeometry(width,height),new T.MeshBasicMaterial({color:new T.Color(power,power,power),side:T.DoubleSide}));
  panel.position.set(...position);panel.lookAt(0,0,0);casingRoom.add(panel);
}
const casingPmrem=new T.PMREMGenerator(renderer),casingEnvironment=casingPmrem.fromScene(casingRoom,.04);
casingPmrem.dispose();casingRoom.traverse(node=>{if(node.isMesh){node.geometry.dispose();node.material.dispose();}});
for(const name of moods){const material=model.getObjectByName('Glass_'+name).material;material.envMap=casingEnvironment.texture;material.envMapIntensity=.5;}
const expressionVariants=moods.map(name=>({name,nodes:['Orb_'+name,'Face_'+name,...['Crown','Chin','Side','RearCap'].map(p=>p+'Trim_'+name)].map(n=>model.getObjectByName(n))}));
// Soft atmosphere is a preview/presentation layer, not a luminous ring mesh.
const glowCanvas=document.createElement('canvas');glowCanvas.width=glowCanvas.height=128;
const glowContext=glowCanvas.getContext('2d'),gradient=glowContext.createRadialGradient(64,64,0,64,64,64);
gradient.addColorStop(0,'rgba(255,255,255,.48)');gradient.addColorStop(.25,'rgba(255,255,255,.26)');gradient.addColorStop(.6,'rgba(255,255,255,.07)');gradient.addColorStop(1,'rgba(255,255,255,0)');
glowContext.fillStyle=gradient;glowContext.fillRect(0,0,128,128);
const glowMap=new T.CanvasTexture(glowCanvas);
const halo=new T.Sprite(new T.SpriteMaterial({map:glowMap,color:palette.default,transparent:true,opacity:.38,depthWrite:false,blending:T.AdditiveBlending}));
halo.position.set(0,-.15,-.8);halo.scale.set(3.8,3.8,1);scene.add(halo);
const pool=new T.Mesh(new T.PlaneGeometry(3,2),new T.MeshBasicMaterial({map:glowMap,color:palette.default,transparent:true,opacity:.6,depthWrite:false,side:T.DoubleSide,blending:T.AdditiveBlending}));
pool.rotation.x=-Math.PI/2;pool.position.set(0,-1.92,0);scene.add(pool);
// The portable GLB keeps per-mood light variants. The interactive viewer
// needs only four three-source strips, recolored directly.
const liveLights=[];
for(const plate of ['Crown','Chin','Side','RearCap']){
  const light=model.getObjectByName(plate+'Underlight_default').clone();
  light.traverse(source=>{if(source.isSpotLight)source.target=source.children.find(child=>child.name===source.name+'Target');});
  for(const name of moods){const variant=model.getObjectByName(plate+'Underlight_'+name);variant.removeFromParent();}
  const mount=new T.Group();mount.name=plate+'LiveLighting';model.getObjectByName(plate).add(mount);mount.add(light);liveLights.push(light);
}
// Keep the face of the casing clear. Soft scattering belongs mainly in the
// deeper edge volume, where it reveals the gap around the opaque inner ball.
// This preview effect follows moving shell lights; glTF retains transmission
// and absorption, but needs renderer-specific scattering.
for(const name of moods){
  const material=model.getObjectByName('Glass_'+name).material;
  material.onBeforeCompile=shader=>{
    shader.uniforms.scatterTint={value:new T.Color(palette[name])};
    shader.uniforms.orbRadii={value:new T.Vector2(orbRadii.outer,orbRadii.inner)};
    shader.fragmentShader='uniform vec3 scatterTint; uniform vec2 orbRadii;\n'+shader.fragmentShader;
    // Underlights represent broad emitters, not a necklace of reflected bulbs.
    // Retain the environment's broad reflections to describe the glass surface.
    shader.fragmentShader=shader.fragmentShader.replace('#include <lights_fragment_end>','#include <lights_fragment_end>\nreflectedLight.directSpecular *= .04;');
    // Blur what is seen through the deeper edge without frosting the smooth
    // reflecting surface or losing the inner ball behind the face.
    shader.fragmentShader=shader.fragmentShader.replace('#include <transmission_fragment>',`
      float depthBlur=smoothstep(.20,.62,1.0-max(dot(normal,normalize(vViewPosition)),0.0));
      material.roughness=mix(material.roughness,.34,depthBlur);
      #include <transmission_fragment>
    `);
    shader.fragmentShader=shader.fragmentShader.replace('#include <opaque_fragment>',`
      float viewCosine=clamp(dot(normal,normalize(vViewPosition)),0.0,1.0);
      float impactSquared=orbRadii.x*orbRadii.x*(1.0-viewCosine*viewCosine);
      float outerDepth=orbRadii.x*viewCosine;
      float innerDepth=sqrt(max(0.0,orbRadii.y*orbRadii.y-impactSquared));
      float opticalPath=outerDepth-innerDepth;
      vec3 scatteredLight=vec3(0.0);
      #if NUM_POINT_LIGHTS > 0
        for(int i=0;i<NUM_POINT_LIGHTS;i++){
          vec3 lightVector=pointLights[i].position+vViewPosition;
          scatteredLight+=pointLights[i].color/(.8+dot(lightVector,lightVector));
        }
      #endif
      #if NUM_SPOT_LIGHTS > 0
        for(int i=0;i<NUM_SPOT_LIGHTS;i++){
          vec3 lightVector=spotLights[i].position+vViewPosition;
          float facing=dot(normalize(lightVector),spotLights[i].direction);
          float spread=getSpotAttenuation(spotLights[i].coneCos,spotLights[i].penumbraCos,facing);
          scatteredLight+=spotLights[i].color*spread/(.8+dot(lightVector,lightVector));
        }
      #endif
      float edgeVolume=smoothstep(.15,.8,1.0-viewCosine);
      outgoingLight+=scatterTint*scatteredLight*.12*edgeVolume*(1.0-exp(-2.8*opticalPath));
      #include <opaque_fragment>
    `);
  };
  material.customProgramCacheKey=()=> 'smarter-marble-edge-scattering-v2';
}
const composer=new EffectComposer(renderer);
composer.addPass(new RenderPass(scene,camera));
const beforeBloom=new SavePass();composer.addPass(beforeBloom);
const eyeMask=new T.WebGLRenderTarget(1,1,{samples:4});
const maskBlack=new T.MeshBasicMaterial({color:0x000000,toneMapped:false});
const maskWhite=new T.MeshBasicMaterial({color:0xffffff,toneMapped:false});
const maskedMeshes=[];model.traverse(mesh=>{if(mesh.isMesh)maskedMeshes.push({mesh,material:mesh.material,mask:/Pupil|Brow/.test(mesh.name)?maskWhite:maskBlack});});
const bloom=new UnrealBloomPass(new T.Vector2(800,600),.42,.6,1.0);
// Preserve a meaningful halo alpha instead of making the PNG background opaque.
bloom.blendMaterial.fragmentShader=bloom.blendMaterial.fragmentShader.replace('gl_FragColor = opacity * texel;','gl_FragColor = vec4(opacity * texel.rgb, clamp(max(texel.r,max(texel.g,texel.b)) * opacity, 0.0, 1.0));');
bloom.blendMaterial.blending=T.CustomBlending;bloom.blendMaterial.blendSrc=T.OneFactor;bloom.blendMaterial.blendDst=T.OneFactor;bloom.blendMaterial.blendSrcAlpha=T.OneFactor;bloom.blendMaterial.blendDstAlpha=T.OneFactor;
composer.addPass(bloom);
const protectEyes=new ShaderPass({
  uniforms:{tDiffuse:{value:null},beforeBloom:{value:null},eyeMask:{value:null}},
  vertexShader:'varying vec2 vUv; void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
  fragmentShader:'uniform sampler2D tDiffuse,beforeBloom,eyeMask; varying vec2 vUv; void main(){float coverage=texture2D(eyeMask,vUv).r;gl_FragColor=mix(texture2D(tDiffuse,vUv),texture2D(beforeBloom,vUv),coverage);}'
});
protectEyes.uniforms.beforeBloom.value=beforeBloom.renderTarget.texture;
protectEyes.uniforms.eyeMask.value=eyeMask.texture;
composer.addPass(protectEyes);composer.addPass(new OutputPass());
function renderScene(){
  setShellState(model,mood,shellSpinLocked?shellMotionTime:action.time,shellCurrent);
  for(const variant of expressionVariants)for(const node of variant.nodes){node.visible=variant.name===mood;node.scale.setScalar(node.visible?1:.001);}
  for(const light of liveLights)light.traverse(source=>{if(source.isLight)source.color.setHex(palette[mood]);});
  halo.material.color.setHex(palette[mood]);pool.material.color.setHex(palette[mood]);
  // Preserve the unbloomed pupils, using a depth-tested mask so eyes never
  // appear through closed plates. Glints and the luminous iris stay unmasked.
  const background=scene.background,target=renderer.getRenderTarget(),clearColor=renderer.getClearColor(new T.Color()),clearAlpha=renderer.getClearAlpha();
  try{
    for(const entry of maskedMeshes)entry.mesh.material=entry.mask;
    halo.visible=pool.visible=false;scene.background=new T.Color(0x000000);
    renderer.setRenderTarget(eyeMask);renderer.render(scene,camera);
  }finally{
    for(const entry of maskedMeshes)entry.mesh.material=entry.material;
    halo.visible=pool.visible=true;scene.background=background;renderer.setClearColor(clearColor,clearAlpha);renderer.setRenderTarget(target);
  }
  composer.render();
}
function sizeEffects(){const size=renderer.getSize(new T.Vector2()),ratio=renderer.getPixelRatio();composer.setPixelRatio(ratio);composer.setSize(size.x,size.y);eyeMask.setSize(size.x*ratio,size.y*ratio);}
const mixer=new T.AnimationMixer(model);let action=mixer.clipAction(clips[0]);action.play();
const controls=new OrbitControls(camera,renderer.domElement);controls.target.set(0,.05,0);controls.enableDamping=true;controls.minDistance=3.5;controls.maxDistance=11;controls.enablePan=false;controls.update();controls.saveState();
let mood='default',playing=!matchMedia('(prefers-reduced-motion: reduce)').matches,speed=1,elapsed=0,recording=false,shellCurrent=1,shellTarget=1;
let shellSpinLocked=false,shellMotionTime=0,shellRestTime=0;
const localSpin=()=>mood==='eureka'||mood==='working';
const spinWindow=()=>mood==='working'?{period:4,start:.4,end:3.6}:{period:2,start:.6,end:1};
function spinAtRest(time){const window=spinWindow(),beat=time%window.period;return beat<=window.start||beat>=window.end;}
const symbols=['◉','⌣','⌁','✧','⌨','☼','⌢','⌯'];
moods.forEach((name,i)=>{const b=document.createElement('button');b.innerHTML=`<span aria-hidden="true">${symbols[i]}</span>${name[0].toUpperCase()+name.slice(1)}`;b.setAttribute('aria-pressed',String(i===0));b.onclick=()=>selectMood(name);$('moods').append(b);});
function selectMood(name){mood=name;elapsed=0;shellSpinLocked=localSpin()&&shellCurrent<.98;shellMotionTime=shellRestTime=0;const next=mixer.clipAction(clips[moods.indexOf(name)]);if(next!==action&&playing&&!document.hidden){action.fadeOut(.3);next.reset().fadeIn(.3).play();}else{mixer.stopAllAction();next.reset().play();}action=next;mixer.update(0);$('mode-label').textContent=name.toUpperCase()+' / LOOP';$('description').textContent=descriptions[moods.indexOf(name)];[...$('moods').children].forEach((b,i)=>b.setAttribute('aria-pressed',String(moods[i]===name)));renderScene();}
selectMood('default');
function syncPlay(){$('play').textContent=playing?'Ⅱ Pause':'▷ Play';$('play').setAttribute('aria-label',playing?'Pause animation':'Play animation');}syncPlay();
$('play').onclick=()=>{playing=!playing;syncPlay();};$('speed').onchange=e=>speed=Number(e.target.value);$('reset').onclick=()=>{controls.reset();$('view').value='three';};
$('view').onchange=()=>{const views={three:[2,1.05,6.8],front:[0,.15,7.1],side:[7.1,.15,0],back:[0,.15,-7.1]};camera.position.set(...views[$('view').value]);controls.update();renderScene();};
function requestShell(target,immediate=false){
  shellTarget=target;
  if(localSpin()&&!shellSpinLocked){
    shellSpinLocked=true;shellMotionTime=action.time;const window=spinWindow();
    shellRestTime=spinAtRest(action.time)?action.time:Math.floor(action.time/window.period)*window.period+window.end;
  }
  if(document.hidden){shellMotionTime=shellRestTime;shellCurrent=target;}
  else if(immediate&&!localSpin())shellCurrent=target;
  $('shell').value=String(target);$('shell-toggle').textContent=target>.5?'Close shell':'Open shell';renderScene();
}
$('shell').oninput=()=>requestShell(Number($('shell').value),true);
$('shell-toggle').onclick=()=>requestShell(shellTarget>.5?0:1);
function resize(){if(recording)return;renderer.setSize(host.clientWidth,host.clientHeight);camera.aspect=host.clientWidth/host.clientHeight;camera.updateProjectionMatrix();sizeEffects();renderScene();}new ResizeObserver(resize).observe(host);resize();$('loading').hidden=true;
const clock=new T.Clock();renderer.setAnimationLoop(()=>{const dt=Math.min(clock.getDelta(),.1);if(recording)return;
  if(shellSpinLocked)shellMotionTime=Math.min(shellRestTime,shellMotionTime+dt*(mood==='working'?4:1));
  if(!shellSpinLocked||shellMotionTime>=shellRestTime)shellCurrent=T.MathUtils.damp(shellCurrent,shellTarget,5,dt);
  if(playing){mixer.update(dt*speed);elapsed+=dt*speed;if($('cycle').checked&&elapsed>=4)selectMood(moods[(moods.indexOf(mood)+1)%8]);}
  // Rejoin the loop during a hold, never halfway through a new spin.
  if(shellSpinLocked&&shellMotionTime>=shellRestTime&&shellCurrent>=.98&&shellTarget>=.98&&spinAtRest(action.time))shellSpinLocked=false;
  controls.update();renderScene();});
function download(blob,name){const a=document.createElement('a'),url=URL.createObjectURL(blob);a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}
function dimensions(){return $('format').value==='wide'?[1920,1080]:$('format').value==='portrait'?[1080,1920]:[1024,1024];}
let savedCamera;
function exportFrame(transparent){const [w,h]=dimensions();savedCamera=camera.position.clone();camera.position.sub(controls.target).normalize().multiplyScalar(7.3*Math.max(1,h/w)).add(controls.target);renderer.setPixelRatio(1);renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();scene.background=transparent?null:new T.Color(0x081820);sizeEffects();renderScene();}
function restore(){if(savedCamera){camera.position.copy(savedCamera);savedCamera=null;}scene.background=null;renderer.setPixelRatio(Math.min(devicePixelRatio,2));resize();}
function lockRecording(locked){controls.enabled=!locked;document.querySelectorAll('button,select,input').forEach(el=>{el.disabled=locked;});}
$('png').onclick=()=>{try{exportFrame($('transparent').checked);renderer.domElement.toBlob(blob=>{if(blob){download(blob,`smarter-${mood}-${$('format').value}.png`);$('status').textContent='Image exported. Transparent backgrounds are preserved in PNG.';}else $('status').textContent='Image export failed. Try again.';},'image/png');}catch(e){$('status').textContent=e.message;}finally{restore();}};
if(!window.MediaRecorder||!renderer.domElement.captureStream){$('record').disabled=true;$('record').textContent='Video unavailable in this browser';}
$('record').onclick=()=>{
  if(recording)return;
  const mime=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/mp4'].find(m=>MediaRecorder.isTypeSupported(m));
  if(!mime){$('status').textContent='No supported video encoder. Use GLB in your video tool.';return;}
  const prev={playing,speed,time:action.time,elapsed,shellSpinLocked};let stream,recordTimer,failed=false,finished=false;
  const finish=()=>{if(finished)return;finished=true;clearInterval(recordTimer);stream?.getTracks().forEach(t=>t.stop());recording=false;playing=prev.playing;speed=prev.speed;elapsed=prev.elapsed;shellSpinLocked=prev.shellSpinLocked;action.time=prev.time;mixer.update(0);lockRecording(false);syncPlay();restore();};
  try{action.time=0;if(shellCurrent>=.98)shellSpinLocked=false;mixer.update(0);exportFrame(false);recording=true;playing=true;speed=1;stream=renderer.domElement.captureStream(30);const recorder=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:12000000}),chunks=[];
    lockRecording(true);$('status').textContent='Recording one 4-second loop…';
    recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
    recorder.onerror=()=>{failed=true;finish();$('status').textContent='Video recording failed. Try another browser.';};
    recorder.onstop=()=>{if(failed)return;const blob=new Blob(chunks,{type:mime});finish();if(!blob.size){$('status').textContent='The encoder returned an empty video. Try another browser.';return;}download(blob,`smarter-${mood}.${mime.includes('mp4')?'mp4':'webm'}`);$('status').textContent='Video exported. Import the GLB for transparent renders or longer trailers.';};
    recorder.start();const started=performance.now();
    recordTimer=setInterval(()=>{action.time=Math.min(3.999,(performance.now()-started)/1000);mixer.update(0);renderScene();},1000/30);
    setTimeout(()=>{if(recorder.state==='recording')recorder.stop();},4000);
  }catch(e){failed=true;finish();$('status').textContent='Recording unavailable: '+e.message;}
};
