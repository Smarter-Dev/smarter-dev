import * as T from 'three';
import { mergeVertices } from 'three/addons/utils/BufferGeometryUtils.js';
export const moods=['default','happy','playful','excited','working','eureka','sad','mad'];
export const descriptions=['A curious little companion. Cyan, calm, and listening.','Mint-green joy, smiling eyes, and a buoyant sway.','Lavender mischief. A quick one-eye blink and a shell flourish.','Peach-pink energy. The shells orbit the orb in quick bursts, with pauses.','Cool turquoise concentration. Slowly turning plates and a gentle reading gaze.','A golden aha. Each shell spins rapidly in place, pauses, then repeats.','A deep blue sigh. A lowered gaze and a gentle slump.','A warm red huff. Ruffled armor, strong opinions.'];
export const palette={default:0x00dcef,happy:0x48efb2,playful:0xb988ff,excited:0xff83b6,working:0x24c8db,eureka:0xffd658,sad:0x3586ed,mad:0xff5a46};
export const orbRadii=Object.freeze({outer:.98,inner:.80});
const TAU=Math.PI*2,sphereZ=(x,y,r)=>Math.sqrt(Math.max(.01,r*r-x*x-y*y));
// One forward turn per two-second beat. Eureka's .4 s burst is snappier
// than Excited's .8 s orbit; quintic easing settles both into a true stop.
function shellSpin(time,duration=.8,period=2,delay=.6){const cycle=Math.floor(time/period),progress=T.MathUtils.clamp((time-cycle*period-delay)/duration,0,1),eased=progress**3*(progress*(progress*6-15)+10);return TAU*(cycle+eased);}
export function createMascot(){
  const root=new T.Group();root.name='Smarter';const body=new T.Group();body.name='Body';root.add(body);
  const armor=new T.Group();armor.name='ArmorRig';body.add(armor);
  const metal=new T.MeshStandardMaterial({color:0x465461,metalness:.48,roughness:.60,vertexColors:true});
  function mesh(name,geometry,material,parent=body){const m=new T.Mesh(geometry,material);m.name=name;parent.add(m);return m;}
  // Four rounded spherical tetrahedron faces tile one sphere with narrow gaps.
  // Plates separate slightly, then sweep back around the orb without stacking.
  function plate(name,triangle){
    const group=new T.Group();group.name=name;armor.add(group);
    const outline=new T.CurvePath(),center=triangle.reduce((sum,v)=>sum.add(v),new T.Vector3()).multiplyScalar(1/3);
    group.userData.openDirection=center.clone().normalize().toArray();
    for(let i=0;i<3;i++){const prev=triangle[(i+2)%3],v=triangle[i],next=triangle[(i+1)%3],enter=v.clone().lerp(prev,.075),leave=v.clone().lerp(next,.075);outline.add(new T.QuadraticBezierCurve3(enter,v,leave));outline.add(new T.LineCurve3(leave,next.clone().lerp(v,.075)));}
    const boundary=outline.getSpacedPoints(128).slice(0,-1),segments=boundary.length,rings=28,positions=[],indices=[],seam=[];
    function surface(q,i,layer){const r=1.16+(layer?-.075:.075)*Math.sqrt(Math.max(0,1-q**12));return center.clone().lerp(boundary[i],q*.982).normalize().multiplyScalar(r);}
    for(let l=0;l<2;l++)for(let j=0;j<=rings;j++)for(let i=0;i<segments;i++)positions.push(...surface(j/rings,i,l));
    const n=(rings+1)*segments;
    for(let l=0;l<2;l++)for(let j=0;j<rings;j++)for(let i=0;i<segments;i++){const a=l*n+j*segments+i,b=l*n+j*segments+(i+1)%segments,c=a+segments,d=b+segments;if(j)indices.push(...(l?[a,c,b]:[a,b,c]));indices.push(...(l?[b,c,d]:[b,d,c]));}
    const a=new T.Vector3().fromArray(positions,indices[0]*3),b=new T.Vector3().fromArray(positions,indices[1]*3),c=new T.Vector3().fromArray(positions,indices[2]*3);
    if(b.sub(a).cross(c.sub(a)).dot(a)<0)for(let k=0;k<indices.length;k+=3)[indices[k+1],indices[k+2]]=[indices[k+2],indices[k+1]];
    const raw=new T.BufferGeometry();raw.setAttribute('position',new T.Float32BufferAttribute(positions,3));raw.setIndex(indices);const geometry=mergeVertices(raw);raw.dispose();geometry.computeVertexNormals();
    const gp=geometry.getAttribute('position'),grainColors=[];
    for(let i=0;i<gp.count;i++){const x=gp.getX(i),y=gp.getY(i),z=gp.getZ(i),grain=Math.sin(x*311+y*173+z*257)*Math.sin(x*199-y*293+z*137),value=.90+.055*grain;grainColors.push(value,value,value);}
    geometry.setAttribute('color',new T.Float32BufferAttribute(grainColors,3));mesh(name+'Armor',geometry,metal,group);
    for(let i=0;i<segments;i++)seam.push(surface(.988,i,1).multiplyScalar(.997));
    const seamGeometry=new T.TubeGeometry(new T.CatmullRomCurve3(seam,true),192,.009,8,true);
    for(const mood of moods){
      const trim=new T.Group();trim.name=name+'Trim_'+mood;group.add(trim);
      mesh(name+'Seam_'+mood,seamGeometry,new T.MeshStandardMaterial({color:palette[mood],emissive:palette[mood],emissiveIntensity:name==='Chin'?.65:.25,roughness:.7}),trim);
      // Inactive lights move beyond their finite range. Scale alone does not
      // disable punctual lights, and animated colors are not portable glTF.
      const lamp=new T.Group();lamp.name=name+'Lighting_'+mood;group.add(lamp);
      // A distributed three-source strip approximates a broad under-shell
      // emitter in both Three.js and portable glTF, without a visible bulb.
      const power=(name==='Chin'?9:name==='RearCap'?1.2:2)/3;
      // Every mood uses the same inward-facing mount beneath the plate center.
      const normal=center.clone().normalize();
      const tangent=new T.Vector3().crossVectors(normal,new T.Vector3(0,1,0)).normalize();
      const makeLight=()=>new T.SpotLight(palette[mood],power,5,Math.PI/2,.85,2);
      const light=makeLight();
      light.name=name+'Underlight_'+mood;light.position.copy(normal).multiplyScalar(1.015);lamp.add(light);
      for(const sign of [-1,1]){const fill=makeLight();fill.name=name+'Diffuse_'+mood+'_'+sign;fill.position.copy(normal).addScaledVector(tangent,sign*.28).normalize().multiplyScalar(1.015).sub(light.position);light.add(fill);}
      {
        // Broad inward-facing emitters cannot illuminate their own backing
        // with a point hotspot. Targets are local -Z for portable glTF spots.
        const sources=[light,...light.children];
        light.quaternion.setFromUnitVectors(new T.Vector3(0,0,1),normal);
        for(const source of sources){
          if(source!==light){const direction=source.position.clone().add(light.position).normalize();source.position.applyQuaternion(light.quaternion.clone().invert());source.quaternion.copy(light.quaternion).invert().multiply(new T.Quaternion().setFromUnitVectors(new T.Vector3(0,0,1),direction));}
          source.target.name=source.name+'Target';source.target.position.set(0,0,-1);source.add(source.target);
        }
      }
    }
  }
  const nose=new T.Vector3(0,0,1),rear=Array.from({length:3},(_,i)=>{const a=.913+i*TAU/3;return new T.Vector3(Math.sqrt(8/9)*Math.cos(a),Math.sqrt(8/9)*Math.sin(a),-1/3);});
  ['Crown','Chin','Side'].forEach((name,i)=>plate(name,[nose,rear[i],rear[(i+1)%3]]));plate('RearCap',[...rear].reverse());
  const coreGeometry=new T.SphereGeometry(orbRadii.inner,96,64),vertices=coreGeometry.getAttribute('position'),colorValues=[];
  for(let i=0;i<vertices.count;i++){const x=vertices.getX(i),y=vertices.getY(i),z=vertices.getZ(i);const grain=Math.sin(x*137+Math.sin(y*109))*Math.sin(z*151-y*97),cloud=Math.sin(x*9+z*7)*Math.cos(y*11-z*4),value=.91+.016*grain+.035*cloud;colorValues.push(value,value,value);}
  coreGeometry.setAttribute('color',new T.Float32BufferAttribute(colorValues,3));const glassGeometry=new T.SphereGeometry(orbRadii.outer,80,56);
  // Color variants use scale tracks, preserving colors without proprietary
  // animated material extensions when the character is imported into Blender.
  for(const mood of moods){const group=new T.Group();group.name='Orb_'+mood;body.add(group);const tint=new T.Color(palette[mood]);
    mesh('Inner_'+mood,coreGeometry,new T.MeshStandardMaterial({color:0x999999,vertexColors:true,roughness:.84,metalness:0}),group);
    // Smooth tinted casing around a matte core: surface frosting hides the
    // separation and makes both layers read as a single opaque ball.
    mesh('Glass_'+mood,glassGeometry,new T.MeshPhysicalMaterial({color:tint.clone().lerp(new T.Color(0xffffff),.45),transmission:1,thickness:.12,ior:1.32,roughness:.16,metalness:0,attenuationColor:tint,attenuationDistance:.85,specularIntensity:.16,clearcoat:0}),group);
  }
  // Thin face patches are projected onto the surface, including their blink
  // morphs. No detached eye spheres or flat mouth planes in profile views.
  const face=new T.Group();face.name='Face';body.add(face);
  const ink=new T.MeshBasicMaterial({color:0x000204,side:T.FrontSide,transparent:true});
  const white=new T.MeshStandardMaterial({color:0xbfffff,emissive:0x6bfaff,emissiveIntensity:1.15,transparent:true,roughness:.65});
  function projectGeometry(geometry,r){const p=geometry.getAttribute('position'),normals=[];for(let i=0;i<p.count;i++){const x=p.getX(i),y=p.getY(i),z=sphereZ(x,y,r);p.setXYZ(i,x,y,z);normals.push(x/r,y/r,z/r);}geometry.setAttribute('normal',new T.Float32BufferAttribute(normals,3));return geometry;}
  function disc(name,x,y,rx,ry,r,material,parent,blink=false){const geometry=new T.RingGeometry(0,1,48,8);geometry.scale(rx,ry,1);geometry.translate(x,y,0);projectGeometry(geometry,r);
    const scanGaze=name.startsWith('working')&&/Pupil|Glint/.test(name),gazeMorphs=[];
    if(blink){const base=geometry.getAttribute('position');geometry.morphAttributes.position=[];
      for(const gaze of scanGaze?[-1,0,1]:[0])for(let step=0;step<=8;step++){
        if(gaze===0&&step===0)continue;
        const closed=[],factor=1-.96*step/8;
        for(let i=0;i<base.count;i++){const px=base.getX(i)+gaze*.044,py=.14+(base.getY(i)-.14)*factor;closed.push(px,py,sphereZ(px,py,r));}
        geometry.morphAttributes.position.push(new T.Float32BufferAttribute(closed,3));gazeMorphs.push([gaze,step]);
      }
    }
    const result=mesh(name,geometry,material,parent);if(scanGaze)result.userData.gazeMorphs=gazeMorphs;return result;
  }
  function stroke(name,points,r,width,material,parent){const curve=new T.CatmullRomCurve3(points.map(([x,y])=>new T.Vector3(x,y,0))),positions=[],indices=[];
    for(let i=0;i<=48;i++){const p=curve.getPoint(i/48),t=curve.getTangent(i/48),n=new T.Vector3(-t.y,t.x,0).multiplyScalar(width);positions.push(...p.clone().add(n),...p.clone().sub(n));if(i<48){const a=i*2;indices.push(a,a+1,a+2,a+1,a+3,a+2);}}
    const geometry=new T.BufferGeometry();geometry.setAttribute('position',new T.Float32BufferAttribute(positions,3));geometry.setIndex(indices);return mesh(name,projectGeometry(geometry,r),material,parent);}
  for(const mood of moods){const group=new T.Group();group.name='Face_'+mood;face.add(group);
    for(const [side,x] of [['Left',-.34],['Right',.34]]){
      if(mood==='happy'){const points=Array.from({length:25},(_,i)=>{const u=i/12-1;return[x+u*.13,.13+.09*(1-u*u)];});stroke(mood+side+'Joy',points,.984,.023,white,group);}
      else{const height=mood==='sad'?.19:mood==='mad'?.16:.22;disc(mood+side+'Eye',x,.14,.148,height,.983,white,group,true);const gaze=mood==='working'?0:.027,py=mood==='sad'?.105:.14;disc(mood+side+'Pupil',x+gaze,py,.092,height*.78,.985,ink,group,true);disc(mood+side+'Glint',x+gaze+.025,py+.065,.025,.032,.987,white,group,true);}
      if(['working','sad','mad'].includes(mood)){const slope=(side==='Left'?1:-1)*(mood==='mad'?-.05:mood==='sad'?.05:0);stroke(mood+side+'Brow',[[x-.11,.43-slope],[x,.44],[x+.11,.43+slope]],.985,.018,ink,group);}
    }
    const bend=['sad','mad'].includes(mood)?.07:mood==='working'?0:-.085;
    if(['excited','eureka'].includes(mood)){const points=Array.from({length:33},(_,i)=>{const a=i/32*TAU;return [.069*Math.cos(a),-.22+.083*Math.sin(a)];});stroke(mood+'Mouth',points,.985,.017,white,group);}
    else{const width=mood==='happy'?.19:.13,points=Array.from({length:25},(_,i)=>{const u=i/12-1;return [u*width,-.20+bend*(1-u*u)];});stroke(mood+'Mouth',points,.985,.017,white,group);}
  }
  root.userData={title:'Smarter orb',units:'meters',forward:'+Z',moods,palette,source:'3D reconstruction: rounded spherical triangles, opaque textured core inside a translucent marble casing, internal underlight.'};pose(root,'default',0);return root;
}
export function pose(root,mood,time){
  const find=n=>root.getObjectByName(n),phase=time/4*TAU,s=Math.sin(phase),c=Math.cos(phase),pulse=(center,width)=>Math.exp(-(((phase-center)/width)**2));
  const body=find('Body');body.position.set(0,.05*s,0);body.rotation.set(.025*s,.065*s,0);body.scale.setScalar(1);
  if(mood==='happy'){body.position.y=.08+.10*(1-c);body.rotation.z=.10*s;}
  if(mood==='playful'){body.rotation.set(.06*s,.18*s,.14*Math.sin(phase+.4));body.position.x=.07*s;}
  if(mood==='excited'){body.position.y=.14+.13*Math.sin(phase*2);body.rotation.z=.055*s;}
  if(mood==='working'){body.rotation.set(.10+.02*Math.sin(phase*2),.08*s,-.025);body.position.y=.015*s;}
  if(mood==='eureka'){const reveal=pulse(3.1,1.05);body.position.y=.025*s+.20*reveal;body.rotation.set(.10-.16*reveal,0,-.10*reveal);}
  if(mood==='sad'){body.position.y=-.16+.025*s;body.rotation.set(.14,-.06,-.10+.025*s);}
  if(mood==='mad'){body.rotation.set(-.04,.05*Math.sin(phase*3)*pulse(2.9,.8),.025*s);body.position.y=.03*s;}
  for(const name of moods){find('Orb_'+name).scale.setScalar(name===mood?1:.001);find('Face_'+name).scale.setScalar(name===mood?1:.001);}
  for(const plate of ['Crown','Chin','Side','RearCap'])for(const name of moods){find(plate+'Trim_'+name).scale.setScalar(name===mood?1:.001);find(plate+'Lighting_'+name).position.set(0,name===mood?0:10000,0);}
  // Match the normal blink's width. Align the wink peak to a baked frame so
  // the eye closes fully instead of lingering in a half-closed expression.
  root.traverse(n=>{if(n.morphTargetInfluences){const wink=mood==='playful'&&n.name.includes('Right')?pulse(1.7/4*TAU,.10):0,step=Math.max(wink,pulse(5.4,.10))*8,lo=Math.floor(step),fraction=step-lo;n.morphTargetInfluences.fill(0);
    if(n.userData.gazeMorphs){const gaze=-Math.cos(phase);n.userData.gazeMorphs.forEach(([gx,blink],i)=>{n.morphTargetInfluences[i]=Math.max(0,1-Math.abs(gaze-gx))*Math.max(0,1-Math.abs(step-blink));});}
    else{if(lo>0)n.morphTargetInfluences[lo-1]=1-fraction;if(lo<8)n.morphTargetInfluences[lo]=fraction;}
  }});
  setShellState(root,mood,time,1);
}
export function setShellState(root,mood,time,amount=1){
  const phase=time/4*TAU,pulse=(center,width)=>Math.exp(-(((phase-center)/width)**2)),localSpin=mood==='eureka'||mood==='working',spinning=mood==='excited'||localSpin;
  const armor=root.getObjectByName('ArmorRig');armor.rotation.set(0,0,mood==='excited'?shellSpin(time):(mood==='playful'?.17*Math.sin(phase):0)*amount);
  ['Crown','Chin','Side','RearCap'].forEach((name,i)=>{const p=root.getObjectByName(name),direction=new T.Vector3().fromArray(p.userData.openDirection);
    const opening=.96+(spinning?0:.02*Math.sin(phase-i*.3));
    const flutter=mood==='playful'?.09*Math.sin(phase+i):mood==='mad'?.065*Math.sin(phase*5+i)*pulse(2.9,.8):0;
    const travel=(opening+flutter)*amount;
    // Shared forward-set opening: the lower plate rises across the orb's
    // chin, while the face still has a clear central window. No mood offsets.
    p.position.copy(direction).multiplyScalar(travel*.44);p.position.z+=.08*amount;
    if(name==='Chin')p.position.add(new T.Vector3(.30,-.04,.30).multiplyScalar(amount));
    if(name==='RearCap')p.position.addScaledVector(direction,.28*amount);
    const axis=new T.Vector3(-direction.y,direction.x,0).normalize();
    // Keep the chin plate facing forward; the crown and side still tuck back.
    p.quaternion.setFromAxisAngle(axis,name==='RearCap'||name==='Chin'?0:.22*amount);p.scale.setScalar(1);
    const angle=localSpin&&amount>=.98?(mood==='working'?shellSpin(time,3.2,4,.4):shellSpin(time,.4)):0;
    if(localSpin){
      // A radial axis runs through each plate's own center, so this twists
      // the plate in place rather than carrying it around the orb. The live
      // shell control finishes the forward turn before leaving full openness.
      p.quaternion.multiply(new T.Quaternion().setFromAxisAngle(direction,angle));
    }
    // The armor turns over a stationary underlight mount. Counter-rotation
    // keeps the sources from sweeping into the face opening or the glass.
    for(const variant of moods)p.getObjectByName(name+'Lighting_'+variant).quaternion.setFromAxisAngle(direction,-angle);
    p.getObjectByName(name+'LiveLighting')?.quaternion.setFromAxisAngle(direction,-angle);
  });
}
export function createClips(root){const nodes=[];root.traverse(n=>{if(n.isGroup||n.morphTargetInfluences)nodes.push(n);});
  const clips=moods.map(mood=>{const times=[],samples=nodes.map(()=>({position:[],quaternion:[],scale:[],morphTargetInfluences:[]}));
    for(let i=0;i<=120;i++){times.push(i/30);pose(root,mood,i===120?0:i/30);nodes.forEach((n,j)=>{if(n.isGroup)for(const key of ['position','quaternion','scale'])n[key].toArray(samples[j][key],samples[j][key].length);if(n.morphTargetInfluences)samples[j].morphTargetInfluences.push(...n.morphTargetInfluences);});}
    const tracks=[];nodes.forEach((n,j)=>{if(n.isGroup)for(const key of ['position','quaternion','scale']){const Track=key==='quaternion'?T.QuaternionKeyframeTrack:T.VectorKeyframeTrack;tracks.push(new Track(n.name+'.'+key,times,samples[j][key]));}if(n.morphTargetInfluences)tracks.push(new T.NumberKeyframeTrack(n.name+'.morphTargetInfluences',times,samples[j].morphTargetInfluences));});return new T.AnimationClip(mood,4,tracks).optimize();});
  for(const name of ['shell_open','shell_close']){const shellNodes=['ArmorRig','Crown','Chin','Side','RearCap'].map(n=>root.getObjectByName(n)),times=[],samples=shellNodes.map(()=>({position:[],quaternion:[],scale:[]}));
    for(let i=0;i<=36;i++){const t=i/36,eased=t*t*(3-2*t);times.push(i/30);setShellState(root,'default',0,name==='shell_open'?eased:1-eased);shellNodes.forEach((n,j)=>{for(const key of ['position','quaternion','scale'])n[key].toArray(samples[j][key],samples[j][key].length);});}
    const tracks=[];shellNodes.forEach((n,j)=>{for(const key of ['position','quaternion','scale']){const Track=key==='quaternion'?T.QuaternionKeyframeTrack:T.VectorKeyframeTrack;tracks.push(new Track(n.name+'.'+key,times,samples[j][key]));}});clips.push(new T.AnimationClip(name,1.2,tracks));
  }pose(root,'default',0);return clips;
}
