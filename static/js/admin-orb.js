/* ============================================================
   Admindockens orb - en raymarchad sfär i drivande rök.

   Laddas BARA för inloggad personal: skriptet ligger i
   website/partials/admin_dock.html, som i sin tur bara renderas när
   show_admin_dock är sant. En besökare hämtar aldrig filen.

   Varför en riktig shader här men inte i redigeringsorbarna: docken är
   EN knapp per sida. Redigeringsorbarna är trettio per sida, och en
   WebGL-kontext per styck är inte rimligt - de är därför ren CSS.

   Faller tillbaka tyst: utan WebGL sätts aldrig .orb-live, så
   .c-admin-dock__orb står kvar med sin CSS-gradient och knappen fungerar
   precis som förut. Ritar inte alls
   vid prefers-reduced-motion (en stillbild räcker) och pausar när fliken
   ligger i bakgrunden.
   ============================================================ */
(function () {
  'use strict';

  var canvas = document.getElementById('admin-orb-canvas');
  if (!canvas) return;

  var gl = canvas.getContext('webgl', { alpha: true, antialias: false, premultipliedAlpha: true });
  if (!gl) return; // CSS-gradienten under canvasen står kvar.

  var VERT = [
    'attribute vec2 aPos;',
    'void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }'
  ].join('\n');

  /* Raymarchad sfär vars yta förskjuts av fbm-brus. Ljussättningen är
     medvetet enkel: en nyckelljuskälla, en kall fyllnad, ett rim som
     tänder siluetten, och en gloria byggd på hur nära strålen passerade.
     Röken är fbm samplat i ett skal utanför ytan och adderat som alfa. */
  var FRAG = [
    'precision highp float;',
    'uniform vec2 uRes;',
    'uniform float uTime;',
    'uniform float uHover;',
    '',
    'float hash(vec3 p){ return fract(sin(dot(p, vec3(127.1, 311.7, 74.7))) * 43758.5453123); }',
    'float noise(vec3 p){',
    '  vec3 i = floor(p), f = fract(p);',
    '  f = f * f * (3.0 - 2.0 * f);',
    '  float n000=hash(i), n100=hash(i+vec3(1,0,0)), n010=hash(i+vec3(0,1,0)), n110=hash(i+vec3(1,1,0));',
    '  float n001=hash(i+vec3(0,0,1)), n101=hash(i+vec3(1,0,1)), n011=hash(i+vec3(0,1,1)), n111=hash(i+vec3(1,1,1));',
    '  return mix(mix(mix(n000,n100,f.x), mix(n010,n110,f.x), f.y),',
    '             mix(mix(n001,n101,f.x), mix(n011,n111,f.x), f.y), f.z);',
    '}',
    'float fbm(vec3 p){',
    '  float v = 0.0, a = 0.5;',
    '  for (int i = 0; i < 4; i++) { v += a * noise(p); p *= 2.02; a *= 0.5; }',
    '  return v;',
    '}',
    '',
    'float flow(vec3 p){ return fbm(p * 2.6 + vec3(0.0, -uTime * 0.16, uTime * 0.09)); }',
    '',
    // Sfärens avstandsfunktion, förskjuten av bruset -> turbulent yta.
    'float map(vec3 p){ return length(p) - 0.60 - (flow(p) - 0.5) * 0.30; }',
    '',
    'vec3 normalAt(vec3 p){',
    '  vec2 e = vec2(0.0025, 0.0);',
    '  return normalize(vec3(map(p+e.xyy)-map(p-e.xyy), map(p+e.yxy)-map(p-e.yxy), map(p+e.yyx)-map(p-e.yyx)));',
    '}',
    '',
    'void main(){',
    '  vec2 uv = (gl_FragCoord.xy * 2.0 - uRes) / uRes.y;',
    '  vec3 ro = vec3(0.0, 0.0, 2.35);',
    '  vec3 rd = normalize(vec3(uv, -1.7));',
    '',
    '  float t = 0.0, glow = 0.0, smoke = 0.0;',
    '  bool hit = false;',
    '  vec3 p = ro;',
    '  for (int i = 0; i < 48; i++) {',
    '    p = ro + rd * t;',
    '    float d = map(p);',
    '    glow += 0.012 / (0.05 + d * d * 9.0);',        // gloria: nära passager lyser
    '    if (d < 0.004) { hit = true; break; }',
    '    if (t > 4.0) break;',
    '    t += max(d * 0.85, 0.012);',
    '  }',
    '',
    // Röken: fbm i ett skal utanför sfären, samplat glest.
    '  for (int i = 0; i < 10; i++) {',
    '    float st = 1.55 + float(i) * 0.085;',
    '    vec3 sp = ro + rd * st;',
    '    float shell = smoothstep(1.05, 0.62, length(sp));',
    '    smoke += shell * max(flow(sp * 1.5) - 0.36, 0.0) * 0.42;',
    '  }',
    '',
    '  vec3 key  = vec3(1.00, 0.78, 0.28);',   // varm nyckel (#ffd23f)
    '  vec3 fill = vec3(1.00, 0.44, 0.09);',   // orange fyllnad (#ff7a18)
    '  vec3 rimC = vec3(1.00, 0.66, 0.16);',   // barnsten (#ff9f1c)
    '',
    '  vec3 col = vec3(0.0);',
    '  float alpha = 0.0;',
    '',
    '  if (hit) {',
    '    vec3 n = normalAt(p);',
    '    vec3 lKey = normalize(vec3(0.55 + uHover * 0.25, 0.75, 0.85));',
    '    vec3 lFill = normalize(vec3(-0.7, -0.35, 0.55));',
    '    float dKey = max(dot(n, lKey), 0.0);',
    '    float dFill = max(dot(n, lFill), 0.0);',
    '    float wrap = 0.5 + 0.5 * dot(n, vec3(0.0, 1.0, 0.0));',      // ambient wrap
    '    float rim = pow(1.0 - max(dot(n, -rd), 0.0), 2.6);',
    '    vec3 h = normalize(lKey - rd);',
    '    float spec = pow(max(dot(n, h), 0.0), 42.0);',
    '    col = key * dKey * 0.95 + fill * dFill * 0.55 + key * wrap * 0.05;',
    '    col += rimC * rim * (1.15 + uHover * 0.45);',
    '    col += vec3(1.0, 0.94, 0.78) * spec * 0.45;',
    '    alpha = 1.0;',
    '  }',
    '',
    '  col += key * glow * 0.40;',
    '  col += fill * smoke * 0.85;',
    '  alpha = max(alpha, min(glow * 0.45 + smoke * 1.0, 1.0));',
    '',
    // Vinjettering mot cirkelns kant så knappen inte får en fyrkantig ruta.
    '  float edge = smoothstep(1.02, 0.72, length(uv));',
    '  alpha *= edge;',
    // Reinhard: varden over 1 komprimeras i stallet for att klippas.
    // Utan detta blev orben en vit klump - rim, glod och rok summerar
    // langt over 1 och en ren gamma-kurva raddar inte det.
    '  col *= 1.6;',                                                  // exponering fore
    '  col = col / (1.0 + col);',
    '  col = pow(max(col, 0.0), vec3(0.4545));',                     // linjart -> sRGB
    '  gl_FragColor = vec4(col * alpha, alpha);',                     // premultiplicerad
    '}'
  ].join('\n');

  function compile(type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null;
  }

  var vs = compile(gl.VERTEX_SHADER, VERT);
  var fs = compile(gl.FRAGMENT_SHADER, FRAG);
  if (!vs || !fs) return;

  var prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return;
  gl.useProgram(prog);

  var buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  var aPos = gl.getAttribLocation(prog, 'aPos');
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

  var uRes = gl.getUniformLocation(prog, 'uRes');
  var uTime = gl.getUniformLocation(prog, 'uTime');
  var uHover = gl.getUniformLocation(prog, 'uHover');

  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA); // premultiplicerad alfa

  function resize() {
    // Taket på 2 är medvetet: knappen är 52 px och en 3x-yta ger ingen
    // synlig skillnad men tre gånger så många fragment att raymarcha.
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var size = Math.round(canvas.clientWidth * dpr) || 1;
    if (canvas.width !== size) { canvas.width = size; canvas.height = size; }
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(uRes, canvas.width, canvas.height);
  }

  var hover = 0, wantHover = 0;
  var dock = document.getElementById('admin-dock');
  if (dock) {
    dock.addEventListener('pointerenter', function () { wantHover = 1; });
    dock.addEventListener('pointerleave', function () { wantHover = 0; });
  }

  var still = window.matchMedia('(prefers-reduced-motion: reduce)');
  var raf = null;

  function frame(now) {
    hover += (wantHover - hover) * 0.08;
    resize();
    gl.uniform1f(uTime, now * 0.001);
    gl.uniform1f(uHover, hover);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (raf !== null) return;
    if (still.matches) { resize(); gl.uniform1f(uTime, 0.0); gl.uniform1f(uHover, 0.0);
                         gl.drawArrays(gl.TRIANGLES, 0, 3); return; }
    raf = requestAnimationFrame(frame);
  }
  function stop() {
    if (raf !== null) { cancelAnimationFrame(raf); raf = null; }
  }

  // Ingen anledning att raymarcha i en flik ingen tittar på.
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop(); else start();
  });
  if (still.addEventListener) {
    still.addEventListener('change', function () { stop(); start(); });
  }

  if (dock) dock.classList.add('orb-live'); // döljer CSS-fallbacken under
  start();
})();
