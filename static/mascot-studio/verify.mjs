import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import validator from 'gltf-validator';
import { AnimationMixer,Box3,Vector3,Raycaster } from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { moods,setShellState,orbRadii } from './model.js';
const bytes=await readFile(new URL('./public/smarter-orb.glb',import.meta.url));
const report=await validator.validateBytes(new Uint8Array(bytes));
console.log('glTF validation:',{errors:report.issues.numErrors,warnings:report.issues.numWarnings});assert.equal(report.issues.numErrors,0);assert.equal(report.issues.numWarnings,0);
const gltf=await new GLTFLoader().parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
assert.deepEqual(gltf.animations.map(c=>c.name),[...moods,'shell_open','shell_close']);
const moodClips=gltf.animations.filter(c=>moods.includes(c.name));
const mixer=new AnimationMixer(gltf.scene),signatures=[];
for(const clip of moodClips){assert.equal(clip.duration,4);const action=mixer.clipAction(clip);action.reset().play();mixer.update(.8);const body=gltf.scene.getObjectByName('Body');signatures.push([...body.position,...body.quaternion].join(','));const bounds=new Box3().setFromObject(gltf.scene).getSize(new Vector3());assert.ok(bounds.toArray().every(n=>Number.isFinite(n)&&n>0&&n<5));const first=body.position.clone();mixer.update(.4);assert.ok(first.distanceTo(body.position)>0||clip.name==='mad');action.stop();}
assert.equal(new Set(signatures).size,8);console.log('All eight exported clips load, animate, remain finite, and have distinct poses.');
// Validate the actual exported surface geometry, including intermediate blinks.
const point=new Vector3();let maxFaceGap=0;
for(const clip of moodClips){const action=mixer.clipAction(clip);action.reset().play();
  for(const time of [.0,.5,1,1.60,1.67,1.70,1.73,1.83,2,2.5,3,3.40,3.44,3.48]){action.time=time;mixer.update(0);const face=gltf.scene.getObjectByName('Face_'+clip.name);assert.ok(face.scale.x>.99);
    face.traverse(node=>{if(!node.isMesh)return;for(let i=0;i<node.geometry.attributes.position.count;i++){node.getVertexPosition(i,point);const radius=point.length();assert.ok(radius>.981&&radius<.988,`${clip.name}: detached or sunken face at radius ${radius}`);maxFaceGap=Math.max(maxFaceGap,radius-.98);}});
    for(const name of moods){assert.equal(gltf.scene.getObjectByName('Orb_'+name).scale.x>.5,name===clip.name);
      for(const plate of ['Crown','Chin','Side','RearCap'])assert.equal(gltf.scene.getObjectByName(plate+'Lighting_'+name).position.length()<1,name===clip.name,'Inactive mood lights must remain out of range');
    }
  }action.stop();
}
assert.equal(gltf.scene.getObjectByName('Idea'),undefined,'Eureka sun must be removed, not just hidden');
assert.equal(gltf.scene.getObjectByName('IdeaLight'),undefined);
assert.ok(gltf.animations.every(clip=>clip.tracks.every(track=>!track.name.startsWith('Idea.'))));
const closure=node=>node.morphTargetInfluences.reduce((sum,value,i)=>sum+value*(i+1)/8,0);
const playful=mixer.clipAction(gltf.animations.find(c=>c.name==='playful'));playful.reset().play();
const right=gltf.scene.getObjectByName('playfulRightEye'),left=gltf.scene.getObjectByName('playfulLeftEye');
playful.time=1.7;mixer.update(0);assert.ok(closure(right)>.99,'Wink must close fully');assert.ok(closure(left)<.001,'Only one eye should wink');
for(const part of ['Pupil','Glint'])assert.ok(Math.abs(closure(gltf.scene.getObjectByName('playfulRight'+part))-closure(right))<1e-6);
for(const time of [1.53,1.87]){playful.time=time;mixer.update(0);assert.ok(closure(right)<.01,'Wink must not linger');}
const blinkWidth=(start,end)=>{let samples=0;for(let t=start;t<end;t+=1/240){playful.time=t;mixer.update(0);if(closure(right)>.05)samples++;}return samples/240;};
const winkDuration=blinkWidth(1.4,2),blinkDuration=blinkWidth(3.15,3.75);
assert.ok(winkDuration>.15&&winkDuration<.26);assert.ok(Math.abs(winkDuration-blinkDuration)<.025,'Wink should have the same pace as a normal blink');playful.stop();
console.log(`Quick one-eye wink verified (${Math.round(winkDuration*1000)} ms); Eureka sun and its tracks are absent.`);
const armor=gltf.scene.getObjectByName('ArmorRig'),shellNames=['Crown','Chin','Side','RearCap'];
for(const mood of ['excited','eureka','working']){
  const action=mixer.clipAction(gltf.animations.find(c=>c.name===mood));action.reset().play();
  // Float32 GLB quaternion keys need normalization before tiny angle checks.
  const sample=time=>{action.time=time;mixer.update(0);return [armor,...shellNames.map(name=>gltf.scene.getObjectByName(name))].map(n=>({position:n.position.clone(),quaternion:n.quaternion.clone().normalize()}));};
  const pauses=mood==='working'?[[.05,.35],[3.65,3.95]]:mood==='eureka'?[[.1,.55],[1.05,2.55],[3.05,3.95]]:[[.1,.55],[1.45,2.55],[3.45,3.95]];
  for(const [a,b] of pauses){const first=sample(a),last=sample(b);first.forEach((state,i)=>{assert.ok(state.position.distanceTo(last[i].position)<1e-6,`${mood}: shell translates during pause`);assert.ok(state.quaternion.angleTo(last[i].quaternion)<1e-5,`${mood}: shell rotates during pause`);});}
  const start=sample(0),middle=sample(mood==='working'?2:mood==='eureka'?.8:1);
  if(mood==='excited'){
    assert.ok(start[0].quaternion.angleTo(middle[0].quaternion)>3,'Excited must orbit the orb');
    for(let i=1;i<start.length;i++)assert.ok(start[i].quaternion.angleTo(middle[i].quaternion)<1e-5,'Excited must not spin individual plates');
  }else{
    assert.ok(start[0].quaternion.angleTo(middle[0].quaternion)<1e-5,`${mood}: must not orbit the whole armor`);
    for(let i=1;i<start.length;i++)assert.ok(start[i].quaternion.angleTo(middle[i].quaternion)>3,`${mood}: every plate must spin`);
    const pivots=()=>shellNames.map(name=>{const p=gltf.scene.getObjectByName(name);return new Vector3().fromArray(p.userData.openDirection).multiplyScalar(1.16).applyQuaternion(p.quaternion).add(p.position);});
    const lights=shellNames.flatMap(name=>{const light=gltf.scene.getObjectByName(name+'Underlight_'+mood);return [light,...light.children.filter(n=>n.isLight)];});
    const mounts=()=>{gltf.scene.updateMatrixWorld(true);return lights.map(light=>armor.worldToLocal(light.getWorldPosition(new Vector3())));};
    sample(0);const centers=pivots(),fixedLights=mounts();
    for(let frame=0;frame<=120;frame++){
      sample(frame/30);pivots().forEach((p,i)=>assert.ok(p.distanceTo(centers[i])<1e-5,`${mood}: plate center must stay in place`));
      mounts().forEach((p,i)=>assert.ok(p.distanceTo(fixedLights[i])<1e-5,`${mood}: recessed light mount must not spin with the plate`));
      for(const name of shellNames){const plate=gltf.scene.getObjectByName(name),light=gltf.scene.getObjectByName(name+'Underlight_'+mood),outward=new Vector3().fromArray(plate.userData.openDirection).transformDirection(plate.matrixWorld);
        for(const source of [light,...light.children.filter(n=>n.isLight)]){
          const ray=new Raycaster(source.getWorldPosition(new Vector3()),outward);
          assert.ok(ray.intersectObject(gltf.scene.getObjectByName(name+'Armor'),false).length,`${mood}: ${source.name} is not covered by its plate`);
          assert.ok(source.isSpotLight&&source.penumbra>=.8,'Local-spin underlights need a broad, soft inward beam');
          const forward=source.target.getWorldPosition(new Vector3()).sub(source.getWorldPosition(new Vector3())).normalize();
          assert.ok(forward.dot(outward)<-.9,'Underlight must point away from its shell backing, into the orb');
        }
      }
    }
  }
  const movingIndex=mood==='excited'?0:1,spinAxis=mood==='excited'?new Vector3(0,0,1):new Vector3().fromArray(gltf.scene.getObjectByName('Crown').userData.openDirection);let previous=sample(0)[movingIndex].quaternion,travel=0;
  for(let frame=1;frame<=480;frame++){const current=sample(frame/120)[movingIndex].quaternion,delta=previous.clone().invert().multiply(current);if(delta.w<0)delta.set(-delta.x,-delta.y,-delta.z,-delta.w);assert.ok(new Vector3(delta.x,delta.y,delta.z).dot(spinAxis)>-1e-6,`${mood}: rotation reversed`);travel+=previous.angleTo(current);previous=current;}
  assert.ok(Math.abs(travel-(mood==='working'?2:4)*Math.PI)<.02,`${mood}: incorrect number of full forward turns`);
  action.stop();
}
console.log('Eureka spins rapidly in place, Working slowly; Excited orbits. Forward-only turns, stationary pauses, and covered fixed underlights verified.');
const working=mixer.clipAction(gltf.animations.find(c=>c.name==='working'));working.reset().play();
const gaze=node=>node.userData.gazeMorphs.reduce((sum,[x],i)=>sum+x*node.morphTargetInfluences[i],0);
for(const [time,expected] of [[0,-1],[1,0],[2,1],[3,0],[3.999,-1]]){
  working.time=time;mixer.update(0);
  for(const side of ['Left','Right'])for(const part of ['Pupil','Glint'])assert.ok(Math.abs(gaze(gltf.scene.getObjectByName('working'+side+part))-expected)<.002,'Reading gaze must scan smoothly in sync');
}
working.stop();console.log('Working pupils and glints scan left–right–left over four seconds while remaining surface-fit through blinks.');
const hues=moods.map(name=>gltf.scene.getObjectByName('Glass_'+name).material.color.getHex());assert.equal(new Set(hues).size,8);
for(const mood of moods){const core=gltf.scene.getObjectByName('Inner_'+mood).material;assert.equal(core.color.r,core.color.g);assert.equal(core.color.g,core.color.b);assert.equal(core.emissive.getHex(),0);
  const bottom=gltf.scene.getObjectByName('ChinUnderlight_'+mood);assert.ok(bottom.isLight);
  assert.ok(bottom.isSpotLight,'Every mood must share the shielded lighting setup');
  assert.equal(bottom.children.filter(n=>n.isLight).length,2,'Underlight must be distributed across three sources');
  const glassMesh=gltf.scene.getObjectByName('Glass_'+mood),glass=glassMesh.material;
  assert.ok(glass.roughness>=.1&&glass.roughness<=.22,'Casing must remain smooth, not frosted');assert.ok(glass.ior>=1.3);assert.ok(glass.thickness>=.1);assert.equal(glass.transmission,1);assert.ok(glass.specularIntensity<=.2);assert.equal(glass.clearcoat,0);
  glassMesh.geometry.computeBoundingSphere();const coreMesh=gltf.scene.getObjectByName('Inner_'+mood);coreMesh.geometry.computeBoundingSphere();
  assert.ok(Math.abs(glassMesh.geometry.boundingSphere.radius-orbRadii.outer)<1e-5);
  assert.ok(Math.abs(coreMesh.geometry.boundingSphere.radius-orbRadii.inner)<1e-5);
  assert.ok(glassMesh.geometry.boundingSphere.radius-coreMesh.geometry.boundingSphere.radius>=.175,'Inner ball must be distinct from the outer marble casing');
  gltf.scene.getObjectByName('Face_'+mood).traverse(node=>{if(!/Pupil|Brow/.test(node.name))return;assert.ok(node.material.isMeshBasicMaterial,'Dark facial details must be unlit');assert.ok(Math.max(node.material.color.r,node.material.color.g,node.material.color.b)<.002,'Pupils must remain near black');});
  for(const plate of ['Crown','Side','RearCap']){const light=gltf.scene.getObjectByName(plate+'Underlight_'+mood);assert.equal(light.color.getHex(),bottom.color.getHex());assert.ok(light.intensity<bottom.intensity);}
  for(const plate of shellNames){const light=gltf.scene.getObjectByName(plate+'Underlight_'+mood),shared=gltf.scene.getObjectByName(plate+'Underlight_default');assert.ok(light.position.distanceTo(shared.position)<1e-6);assert.ok(light.quaternion.angleTo(shared.quaternion)<1e-5);assert.equal(light.angle,shared.angle);assert.equal(light.intensity,shared.intensity);}
}
console.log('Smooth transmitting casings, separate gray cores, and near-black unlit pupils survive export in every mood.');
console.log(`Surface-fit checks pass (maximum face offset ${(maxFaceGap*1000).toFixed(1)} mm); eight colors and excited shell orbit survive GLB export.`);
const close=mixer.clipAction(gltf.animations.find(c=>c.name==='shell_close'));close.play();close.time=1.199;mixer.update(0);gltf.scene.updateMatrixWorld(true);
const plates=['Crown','Chin','Side','RearCap'].map(n=>gltf.scene.getObjectByName(n+'Armor')),ray=new Raycaster(),origin=new Vector3();let covered=0;
for(let i=0;i<512;i++){const y=1-2*(i+.5)/512,r=Math.sqrt(1-y*y),a=i*2.399963229728653;ray.set(origin,new Vector3(r*Math.cos(a),y,r*Math.sin(a)));if(ray.intersectObjects(plates,false).length)covered++;}
assert.ok(covered/512>.90,`Closed shell covers only ${covered/512*100}%`);close.stop();
const open=mixer.clipAction(gltf.animations.find(c=>c.name==='shell_open'));open.play();open.time=1.199;mixer.update(0);gltf.scene.updateMatrixWorld(true);let visible=0;
for(let i=0;i<9;i++)for(let j=0;j<9;j++){const x=-.5+i/8,y=-.30+j/8*.78,target=new Vector3(x,y,Math.sqrt(.98**2-x*x-y*y));ray.set(new Vector3(x,y,5),new Vector3(0,0,-1));ray.far=5-target.z;if(!ray.intersectObjects(plates,false).length)visible++;}
assert.ok(visible/81>.90,`Open shell reveals only ${visible/81*100}% of face`);
for(const mood of moods)gltf.scene.getObjectByName('Face_'+mood).traverse(node=>{if(!node.isMesh)return;for(let i=0;i<node.geometry.attributes.position.count;i+=Math.max(1,Math.floor(node.geometry.attributes.position.count/32))){node.getVertexPosition(i,point);ray.set(new Vector3(point.x,point.y,5),new Vector3(0,0,-1));ray.far=5-point.z;assert.equal(ray.intersectObjects(plates,false).length,0,`${node.name}: chin shield must not cover the facial features`);}});
let chinCovered=0;for(const x of [-.2,0,.2])for(const y of [-.6,-.7,-.8]){ray.set(new Vector3(x,y,5),new Vector3(0,0,-1));ray.far=5-Math.sqrt(.98**2-x*x-y*y);if(ray.intersectObject(gltf.scene.getObjectByName('ChinArmor'),false).length)chinCovered++;}assert.ok(chinCovered>=6,'The bottom plate should cover the chin, not sit behind it');open.stop();
console.log('Shared shell/light layout verified; chin coverage retains clear eyes and mouth in all eight moods.');
console.log(`Closed shell coverage ${(covered/512*100).toFixed(1)}%; open face visibility ${(visible/81*100).toFixed(1)}%.`);
// Each plate stays on its own side of the pairwise separating planes.
// Test exported motion, not just the procedural rig's endpoints.
function assertSeparated(label){
    for(let i=0;i<plates.length;i++)for(let j=i+1;j<plates.length;j++){
      const a=plates[i].parent,b=plates[j].parent,da=new Vector3().fromArray(a.userData.openDirection),db=new Vector3().fromArray(b.userData.openDirection);
      // A forward-set chin changes the separating-plane direction. Either
      // radial or actual-center plane is a valid proof of disjoint geometry.
      const axes=[da.clone().sub(db).normalize(),da.multiplyScalar(1.16).applyQuaternion(a.quaternion).add(a.position).sub(db.multiplyScalar(1.16).applyQuaternion(b.quaternion).add(b.position)).normalize()];
      const extent=(mesh,minimum,axis)=>{let bound=minimum?Infinity:-Infinity;const p=mesh.geometry.attributes.position;for(let k=0;k<p.count;k++){point.fromBufferAttribute(p,k).applyQuaternion(mesh.parent.quaternion).multiply(mesh.parent.scale).add(mesh.parent.position);const value=point.dot(axis);bound=minimum?Math.min(bound,value):Math.max(bound,value);}return bound;};
      assert.ok(axes.some(axis=>extent(plates[i],true,axis)>extent(plates[j],false,axis)),`${label}: no separating plane for ${a.name}/${b.name}`);
      assert.ok(a.scale.distanceTo(new Vector3(1,1,1))<1e-6,'Plate thickness changed during animation');
    }
}
for(const clip of gltf.animations){const action=mixer.clipAction(clip);action.reset().play();
  for(let frame=0;frame<=36;frame++){action.time=clip.duration*frame/36;mixer.update(0);assertSeparated(`${clip.name} at ${action.time}`);}action.stop();
}
console.log('All ten clips preserve plate size and separation throughout their sampled motion.');
for(const mood of ['excited','eureka','working'])for(const openness of [0,.25,.5,.75,.9,.92,.94,.96,.98,1]){
  setShellState(gltf.scene,mood,0,openness);const nodes=[armor,...shellNames.map(name=>gltf.scene.getObjectByName(name))],start=nodes.map(n=>n.quaternion.clone());
  for(let frame=0;frame<=48;frame++){setShellState(gltf.scene,mood,frame/12,openness);assertSeparated(`${mood}, openness ${openness}, time ${frame/12}`);}
  nodes.forEach((n,i)=>assert.ok(start[i].angleTo(n.quaternion)<1e-5,'Partial-open spin jumps at the loop boundary'));
}
console.log('All three rotating moods preserve separation and loop continuity at all ten tested shell openness settings.');
