/* ============================================================
   SKYLRK-stil gradientbakgrund — exakt samma teknik:
   WebGL-canvas med "lava lamp"-shader (Shadertoy DdcfzH) +
   statiskt filmkorn (hash-brus) subtraherat per pixel.
   Fyra färger härleds ur EN hex per sida via HSL-regler,
   och tweenas när man byter sida. Textfärg väljs automatiskt:
   mörk text om luminansen på topColor > 0.4.
   ============================================================ */

const PAGE_COLORS = {
  hem:            '#f7fcff',  // ljus isblå
  tjanster:       '#5252CC',  // lila-blå
  webbutveckling: '#2456b0',  // blå
  automation:     '#c05a2e',  // bränd orange
  content:        '#a8842a',  // guld/ockra
  hosting:        '#0e3a52',  // mörk petrol
  domain:         '#4a2d73',  // violett
  epost:          '#813a63',  // plommon
  portfolio:      '#2f6f4f',  // djupgrön
  paket:          '#121111',  // nästan svart
  kontakt:        '#7a2b35'   // vinröd
};
const TRANSITION_MS = 900;

/* SEO per "sida" (mockup: på riktig sajt = egna URL:er + server-renderade meta) */
const PAGE_META = {
  hem:            {title:'ADX — Digitalbyrå i Stockholm', desc:'ADX är en digitalbyrå i Stockholm: webbutveckling, automation, managed content, hosting, domäner och e-post. Ett team, ett ansvar.'},
  tjanster:       {title:'Tjänster — Webb, automation, drift & innehåll | ADX', desc:'Alla ADX tjänster: webbutveckling, automation & integrationer, managed content, managed hosting, domain management och e-post.'},
  webbutveckling: {title:'Webbutveckling Stockholm — färdiga paket & skräddarsytt | ADX', desc:'Webbutveckling i Stockholm: färdiga paket och skräddarsydda webbplatser och intranät. Snabba, säkra och byggda för att förvaltas över tid.'},
  automation:     {title:'Automation & API-integrationer — effektivisera era processer | ADX', desc:'Effektivisera interna processer med skript, API-integrationer och automatiserade arbetsflöden. Kartläggning, bygge och övervakning av ADX i Stockholm.'},
  content:        {title:'Managed Content — copywriting & SEO som löpande tjänst | ADX', desc:'Copywriting och sökmotoroptimering som gör att rätt kunder hittar er och stannar. Sökordsanalys, innehåll och månadsrapportering från ADX.'},
  hosting:        {title:'Managed Hosting & molntjänster — trygg drift & multi-tenant | ADX', desc:'Managed hosting med övervakning 24/7, dagliga backuper och SSL. Multi-tenant-lösningar för kedjor. 99,9 % drifttid från ADX i Stockholm.'},
  domain:         {title:'Domain Management — domäner, DNS & bevakning | ADX', desc:'Registrering, överlåtelser, DNS-konfiguration och bevakning av varumärken och HTTPS/SSL-certifikat. Full kontroll på era domäner med ADX.'},
  epost:          {title:'E-post för företag — Office 365 & Google Workspace | ADX', desc:'Uppsättning, licenshantering, användaradministration och säkerhet för Office 365 och Google Workspace. Migrering utan avbrott med ADX.'},
  portfolio:      {title:'Portfolio — utvalda projekt | ADX', desc:'Ett urval av webbplatser, plattformar och automationsflöden som ADX byggt och förvaltar.'},
  paket:          {title:'Paket & priser — Start, Tillväxt, Enterprise | ADX', desc:'Jämför ADX paket: Start, Tillväxt och Enterprise. Webbplats, hosting, SEO, automation och multi-tenant-lösningar för kedjor.'},
  kontakt:        {title:'Kontakt — skicka en förfrågan | ADX', desc:'Kontakta ADX, digitalbyrå i Stockholm. Berätta var ni står och vart ni vill — första mötet kostar ingenting.'}
};

function hexToRgb(hex){
  const e = hex.replace('#','');
  return [parseInt(e.slice(0,2),16)/255, parseInt(e.slice(2,4),16)/255, parseInt(e.slice(4,6),16)/255];
}
function rgbToHsl(r,g,b){
  const max=Math.max(r,g,b), min=Math.min(r,g,b), l=(max+min)/2;
  if(max===min) return [0,0,l];
  const d=max-min, s=l>.5? d/(2-max-min): d/(max+min);
  let h=0;
  if(max===r) h=((g-b)/d+(g<b?6:0))/6;
  else if(max===g) h=((b-r)/d+2)/6;
  else h=((r-g)/d+4)/6;
  return [h*360,s,l];
}
function hslToRgb(h,s,l){
  h/=360;
  if(s===0) return [l,l,l];
  const q=l<.5? l*(1+s): l+s-l*s, p=2*l-q;
  const f=t=>{ if(t<0)t+=1; if(t>1)t-=1;
    return t<1/6? p+(q-p)*6*t : t<1/2? q : t<2/3? p+(q-p)*(2/3-t)*6 : p; };
  return [f(h+1/3), f(h), f(h-1/3)];
}
function paletteFromHex(hex){
  const [r,g,b]=hexToRgb(hex), [h,s,l]=rgbToHsl(r,g,b);
  return {
    top:    hslToRgb(h, s*.85, Math.min(l+.25,.78)),
    bottom: hslToRgb(h, s*.95, Math.max(l-.2,.22)),
    accent: hslToRgb(h, s*.9,  l),
    dark:   hslToRgb(h, s*.7,  .18)
  };
}
function shouldTextBeDark(rgb){
  const f=c=> c<=.03928? c/12.92 : Math.pow((c+.055)/1.055, 2.4);
  return .2126*f(rgb[0]) + .7152*f(rgb[1]) + .0722*f(rgb[2]) > .4;
}

const canvas = document.getElementById('gradient-canvas');
const gl = canvas.getContext('webgl', {antialias:true});

const VERT = `
attribute vec2 aPos;
varying vec2 vTextureCoord;
void main(){
  vTextureCoord = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

/* Fragmentshadern — ordagrant samma som SKYLRK:s "lava-lamp"
   (Shadertoy DdcfzH av welches), inkl. filmGrainIntensity 0.1 */
const FRAG = `
precision highp float;

uniform float uTime;
uniform vec2 uResolution;
uniform vec3 uTopColor;
uniform vec3 uBottomColor;
uniform vec3 uAccentColor;
uniform vec3 uDarkColor;
uniform vec2 uFocusPoint;
uniform float uFocusStrength;

varying vec2 vTextureCoord;

#define filmGrainIntensity 0.1

mat2 Rot(float a) {
  float s = sin(a);
  float c = cos(a);
  return mat2(c, -s, s, c);
}

vec2 hash(vec2 p) {
  p = vec2(dot(p, vec2(2127.1, 81.17)), dot(p, vec2(1269.5, 283.37)));
  return fract(sin(p) * 43758.5453);
}

float noise(in vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float n = mix(mix(dot(-1.0 + 2.0 * hash(i + vec2(0.0, 0.0)), f - vec2(0.0, 0.0)),
                    dot(-1.0 + 2.0 * hash(i + vec2(1.0, 0.0)), f - vec2(1.0, 0.0)), u.x),
                mix(dot(-1.0 + 2.0 * hash(i + vec2(0.0, 1.0)), f - vec2(0.0, 1.0)),
                    dot(-1.0 + 2.0 * hash(i + vec2(1.0, 1.0)), f - vec2(1.0, 1.0)), u.x), u.y);
  return 0.5 + 0.5 * n;
}

float filmGrainNoise(in vec2 uv) {
  return length(hash(vec2(uv.x, uv.y)));
}

void main() {
  vec2 uv = vTextureCoord;
  float aspectRatio = uResolution.x / uResolution.y;

  vec2 tuv = uv - 0.5;

  float t = uTime * 0.5;

  float degree = noise(vec2(t * 0.05, tuv.x * tuv.y));
  tuv.y *= 1.0 / aspectRatio;
  tuv *= Rot(radians((degree - 0.5) * 720.0 + 180.0));
  tuv.y *= aspectRatio;

  float frequency = 5.0;
  float amplitude = 30.0;
  float speed = t * 2.0;
  tuv.x += sin(tuv.y * frequency + speed) / amplitude;
  tuv.y += sin(tuv.x * frequency * 1.5 + speed) / (amplitude * 0.5);

  vec3 color1 = uTopColor;
  vec3 color2 = uDarkColor;
  vec3 color3 = uAccentColor;
  vec3 color4 = uBottomColor;

  vec3 layer1 = mix(color3, color2, smoothstep(-0.3, 0.2, (tuv * Rot(radians(-5.0))).x));
  vec3 layer2 = mix(color4, color1, smoothstep(-0.3, 0.2, (tuv * Rot(radians(-5.0))).x));
  vec3 flatColor = mix(layer1, layer2, smoothstep(0.5, -0.3, tuv.y));

  vec2 warp = tuv - (uv - 0.5);
  vec2 focusPt = uFocusPoint - 0.5;
  vec2 delta = (uv - 0.5) - focusPt + warp * 0.5;
  delta.x *= aspectRatio;
  float d = length(delta);

  float radialMask = smoothstep(0.6, 0.0, d) * uFocusStrength;
  vec3 col = mix(flatColor, mix(flatColor, color1, 0.45), radialMask);

  col = col - filmGrainNoise(uv) * filmGrainIntensity;

  gl_FragColor = vec4(col, 1.0);
}`;

function makeShader(type, src){
  const s = gl.createShader(type);
  gl.shaderSource(s, src); gl.compileShader(s);
  return s;
}
const prog = gl.createProgram();
gl.attachShader(prog, makeShader(gl.VERTEX_SHADER, VERT));
gl.attachShader(prog, makeShader(gl.FRAGMENT_SHADER, FRAG));
gl.linkProgram(prog); gl.useProgram(prog);

const buf = gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER, buf);
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 3,-1, -1,3]), gl.STATIC_DRAW);
const aPos = gl.getAttribLocation(prog, 'aPos');
gl.enableVertexAttribArray(aPos);
gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

const U = {};
for (const n of ['uTime','uResolution','uTopColor','uBottomColor','uAccentColor','uDarkColor','uFocusPoint','uFocusStrength'])
  U[n] = gl.getUniformLocation(prog, n);

function resize(){
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width  = Math.floor(window.innerWidth  * dpr);
  canvas.height = Math.floor(window.innerHeight * dpr);
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.uniform2f(U.uResolution, canvas.width, canvas.height);
}
window.addEventListener('resize', resize);
resize();

let current = paletteFromHex(PAGE_COLORS.hem);
let from = current, to = current, tStart = 0, tDur = 0;
const easeInOut = x => x<.5 ? 2*x*x : 1-Math.pow(-2*x+2,2)/2;
const lerp3 = (a,b,k)=>[a[0]+(b[0]-a[0])*k, a[1]+(b[1]-a[1])*k, a[2]+(b[2]-a[2])*k];

function setGradient(hex, ms){
  from = current;
  to = paletteFromHex(hex);
  if (ms > 0){
    tStart = performance.now(); tDur = ms;
  } else {
    current = to; tDur = 0; // direktbyte (t.ex. vid sidladdning på djuplänk)
  }
  document.body.classList.toggle('text-dark',  shouldTextBeDark(to.top));
  document.body.classList.toggle('text-light', !shouldTextBeDark(to.top));
}

let focusTarget = null, focusPt = [.5,.5], focusStr = 0;
window.addEventListener('pointermove', e=>{
  focusTarget = [e.clientX/window.innerWidth, 1 - e.clientY/window.innerHeight];
});
window.addEventListener('pointerleave', ()=>{ focusTarget = null; });

const t0 = performance.now();
function frame(now){
  if (tDur > 0){
    const k = Math.min((now - tStart)/tDur, 1);
    const e = easeInOut(k);
    current = {
      top:    lerp3(from.top, to.top, e),
      bottom: lerp3(from.bottom, to.bottom, e),
      accent: lerp3(from.accent, to.accent, e),
      dark:   lerp3(from.dark, to.dark, e)
    };
    if (k>=1){ tDur = 0; current = to; }
  }
  const tgt = focusTarget || [.5,.5];
  const tgtStr = focusTarget ? 1 : 0;
  focusPt[0] += (tgt[0]-focusPt[0])*.04;
  focusPt[1] += (tgt[1]-focusPt[1])*.04;
  focusStr   += (tgtStr-focusStr)*.04;

  gl.uniform1f(U.uTime, (now - t0)/1000);
  gl.uniform3fv(U.uTopColor, current.top);
  gl.uniform3fv(U.uBottomColor, current.bottom);
  gl.uniform3fv(U.uAccentColor, current.accent);
  gl.uniform3fv(U.uDarkColor, current.dark);
  gl.uniform2fv(U.uFocusPoint, focusPt);
  gl.uniform1f(U.uFocusStrength, focusStr);
  gl.drawArrays(gl.TRIANGLES, 0, 3);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

/* ---- "Sid"-navigering + mobilmeny + SEO-meta ---- */
const mobileMenu = document.getElementById('mobileMenu');
document.getElementById('menuOpen').addEventListener('click', ()=> mobileMenu.classList.add('open'));
document.getElementById('menuClose').addEventListener('click', ()=> mobileMenu.classList.remove('open'));

/* Huvudmenyns markering: tjänstesidor räknas till "Tjänster" */
const NAV_PARENT = {webbutveckling:'tjanster', automation:'tjanster', content:'tjanster',
                    hosting:'tjanster', domain:'tjanster', epost:'tjanster'};

function goTo(page, animate){
  if (!PAGE_COLORS[page]) page = 'hem';
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.getElementById('page-'+page).classList.add('active');
  const navKey = NAV_PARENT[page] || page;
  document.querySelectorAll('.nav-links a, .mobile-menu nav a').forEach(a=>
    a.classList.toggle('active', a.dataset.nav===navKey));
  setGradient(PAGE_COLORS[page], animate ? TRANSITION_MS : 0);
  const meta = PAGE_META[page];
  document.title = meta.title;
  document.querySelector('meta[name="description"]').setAttribute('content', meta.desc);
  mobileMenu.classList.remove('open');
  window.scrollTo(0,0);
}
/* Länkarna sätter bara location.hash (vanliga <a href="#...">) —
   hashchange-lyssnaren gör själva sidbytet, så att även webbläsarens
   bakåt/framåt-knappar fungerar. Inget element har id som krockar med
   hasharna (sidorna heter id="page-..."), så webbläsaren scrollhoppar inte. */
window.addEventListener('hashchange', ()=> goTo((location.hash||'#hem').slice(1), true));
goTo((location.hash||'#hem').slice(1), false);
