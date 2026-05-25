package com.example.firehello;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.opengl.GLES20;
import android.opengl.GLSurfaceView;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Surface;
import android.view.View;
import android.view.WindowManager;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import javax.microedition.khronos.egl.EGLConfig;
import javax.microedition.khronos.opengles.GL10;

public class BitmapDisplayActivity extends Activity {
    private static final String TAG = "BitmapDisplay";
    private static final int MAX_TEXTURES = 16;
    private static final int CAM_PERM = 1;

    // display protocol (port 8888)
    private static final int DISP_PORT = 8888;
    private static final int CMD_PING       = 0x01;
    private static final int CMD_LOAD_RGBA  = 0x02;
    private static final int CMD_SHOW       = 0x03;
    private static final int CMD_LOAD_COLOR = 0x04;
    private static final int CMD_INFO       = 0x05;
    private static final int CMD_HIGHLIGHT       = 0x06;
    private static final int CMD_CLEAR_HIGHLIGHTS = 0x07;
    private static final int RSP_PONG   = 0x81;
    private static final int RSP_LOADED = 0x82;
    private static final int RSP_SHOWN  = 0x83;
    private static final int RSP_ERROR  = 0x84;
    private static final int RSP_INFO   = 0x85;
    private static final int RSP_HIGHLIGHT_ACK = 0x86;

    // camera stream (port 7777)
    private static final int CAM_PORT    = 7777;
    private static final int CAM_WIDTH   = 640;
    private static final int CAM_HEIGHT  = 480;
    private static final int CAM_FPS     = 30;
    private static final int CAM_BITRATE = 2_000_000;

    private volatile boolean running;
    private int screenWidth, screenHeight;

    // ── display ──
    private GLSurfaceView glView;
    private FullScreenRenderer renderer;
    private ServerSocket dispServerSocket;
    private volatile int pendingTexture = -1;
    private volatile long pendingShowTimeNs;
    private final ConcurrentLinkedQueue<TexLoad> loadQueue = new ConcurrentLinkedQueue<>();
    private final LinkedBlockingQueue<byte[]> responseQueue = new LinkedBlockingQueue<>();
    private final ConcurrentLinkedQueue<HighlightCmd> highlightQueue = new ConcurrentLinkedQueue<>();

    // ── camera ──
    private HandlerThread camThread;
    private Handler camHandler;
    private CameraDevice cameraDevice;
    private CameraCaptureSession captureSession;
    private MediaCodec encoder;
    private Surface encoderInput;
    private ServerSocket camServerSocket;
    private volatile Socket camClient;
    private volatile OutputStream camOut;
    private byte[] codecConfig;

    // ───────────────────────────────────── lifecycle ──

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        applyImmersive();
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = 1.0f;
        getWindow().setAttributes(lp);

        DisplayMetrics dm = new DisplayMetrics();
        getWindowManager().getDefaultDisplay().getRealMetrics(dm);
        screenWidth = dm.widthPixels;
        screenHeight = dm.heightPixels;

        renderer = new FullScreenRenderer();
        glView = new GLSurfaceView(this);
        glView.setEGLContextClientVersion(2);
        glView.setRenderer(renderer);
        glView.setRenderMode(GLSurfaceView.RENDERMODE_CONTINUOUSLY);
        setContentView(glView);

        running = true;
        new Thread(this::runDispServer, "disp-server").start();

        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, CAM_PERM);
        } else {
            startCamera();
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] grants) {
        if (code == CAM_PERM && grants.length > 0
                && grants[0] == PackageManager.PERMISSION_GRANTED) {
            startCamera();
        } else {
            Log.w(TAG, "Camera permission denied — display-only mode");
        }
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) applyImmersive();
    }

    private void applyImmersive() {
        getWindow().getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    @Override protected void onResume() { super.onResume(); glView.onResume(); }
    @Override protected void onPause()  { super.onPause();  glView.onPause();  }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        running = false;
        try { if (captureSession != null) captureSession.close(); } catch (Exception ignored) {}
        try { if (cameraDevice != null) cameraDevice.close(); } catch (Exception ignored) {}
        try { if (encoder != null) { encoder.stop(); encoder.release(); } } catch (Exception ignored) {}
        try { if (camServerSocket != null) camServerSocket.close(); } catch (IOException ignored) {}
        try { if (camClient != null) camClient.close(); } catch (IOException ignored) {}
        try { if (dispServerSocket != null) dispServerSocket.close(); } catch (IOException ignored) {}
        if (camThread != null) camThread.quitSafely();
    }

    // ═══════════════════════════════════ DISPLAY PROTOCOL ══

    private void runDispServer() {
        try {
            dispServerSocket = new ServerSocket(DISP_PORT);
            Log.i(TAG, "Display server on port " + DISP_PORT);
            while (running) {
                try {
                    Socket c = dispServerSocket.accept();
                    c.setTcpNoDelay(true);
                    Log.i(TAG, "Display client connected");
                    new Thread(() -> handleDispClient(c), "disp-client").start();
                } catch (IOException e) {
                    if (running) Log.w(TAG, "disp accept", e);
                }
            }
        } catch (IOException e) {
            Log.e(TAG, "Display server failed", e);
        }
    }

    private void handleDispClient(Socket client) {
        try {
            InputStream in = client.getInputStream();
            OutputStream out = client.getOutputStream();
            while (running && !client.isClosed()) {
                int cmd = in.read();
                if (cmd == -1) break;
                int len = readIntLE(in);
                if (len < 0 || len > 50_000_000) throw new IOException("bad length");
                byte[] payload = len > 0 ? readExact(in, len) : new byte[0];
                switch (cmd) {
                    case CMD_PING:       handlePing(out, payload);      break;
                    case CMD_INFO:       handleInfo(out);               break;
                    case CMD_LOAD_COLOR: handleLoadColor(out, payload); break;
                    case CMD_LOAD_RGBA:  handleLoadRgba(out, payload);  break;
                    case CMD_SHOW:       handleShow(out, payload);      break;
                    case CMD_HIGHLIGHT:       handleHighlight(out, payload);      break;
                    case CMD_CLEAR_HIGHLIGHTS: handleClearHighlights(out, payload); break;
                    default:
                        sendResp(out, RSP_ERROR, ("unknown cmd " + cmd).getBytes());
                }
            }
        } catch (IOException e) {
            Log.i(TAG, "Display client gone: " + e.getMessage());
        } finally {
            responseQueue.clear();
            highlightQueue.clear();
            pendingTexture = -1;
            try { client.close(); } catch (IOException ignored) {}
        }
    }

    private void handlePing(OutputStream out, byte[] payload) throws IOException {
        long t = System.nanoTime();
        ByteBuffer r = ByteBuffer.allocate(16).order(ByteOrder.LITTLE_ENDIAN);
        r.put(payload, 0, 8);
        r.putLong(t);
        sendResp(out, RSP_PONG, r.array());
    }

    private void handleInfo(OutputStream out) throws IOException {
        ByteBuffer r = ByteBuffer.allocate(16).order(ByteOrder.LITTLE_ENDIAN);
        r.putInt(screenWidth);
        r.putInt(screenHeight);
        r.putLong(renderer.getFramePeriodNs());
        sendResp(out, RSP_INFO, r.array());
    }

    private void handleLoadColor(OutputStream out, byte[] payload) throws IOException {
        ByteBuffer bb = ByteBuffer.wrap(payload).order(ByteOrder.LITTLE_ENDIAN);
        int id = bb.getInt();
        byte cr = bb.get(), cg = bb.get(), cb = bb.get(), ca = bb.get();
        if (id < 0 || id >= MAX_TEXTURES) {
            sendResp(out, RSP_ERROR, "bad id".getBytes()); return;
        }
        loadQueue.add(new TexLoad(id, 1, 1, new byte[]{cr, cg, cb, ca}));
        awaitAndForward(out, RSP_LOADED, 5);
    }

    private void handleLoadRgba(OutputStream out, byte[] payload) throws IOException {
        ByteBuffer bb = ByteBuffer.wrap(payload).order(ByteOrder.LITTLE_ENDIAN);
        int id = bb.getInt(), w = bb.getInt(), h = bb.getInt();
        if (id < 0 || id >= MAX_TEXTURES) {
            sendResp(out, RSP_ERROR, "bad id".getBytes()); return;
        }
        int expected = w * h * 4;
        if (bb.remaining() != expected) {
            sendResp(out, RSP_ERROR,
                ("size mismatch: want " + expected + " got " + bb.remaining()).getBytes());
            return;
        }
        byte[] rgba = new byte[expected];
        bb.get(rgba);
        loadQueue.add(new TexLoad(id, w, h, rgba));
        awaitAndForward(out, RSP_LOADED, 10);
    }

    private void handleShow(OutputStream out, byte[] payload) throws IOException {
        ByteBuffer bb = ByteBuffer.wrap(payload).order(ByteOrder.LITTLE_ENDIAN);
        int id = bb.getInt();
        long targetNs = bb.getLong();
        pendingShowTimeNs = targetNs;
        pendingTexture = id;
        awaitAndForward(out, RSP_SHOWN, 5);
    }

    private void handleHighlight(OutputStream out, byte[] payload) throws IOException {
        ByteBuffer bb = ByteBuffer.wrap(payload).order(ByteOrder.LITTLE_ENDIAN);
        HighlightCmd hc = new HighlightCmd();
        hc.x1 = bb.getFloat(); hc.y1 = bb.getFloat();
        hc.x2 = bb.getFloat(); hc.y2 = bb.getFloat();
        hc.r = (bb.get() & 0xFF) / 255f;
        hc.g = (bb.get() & 0xFF) / 255f;
        hc.b = (bb.get() & 0xFF) / 255f;
        hc.a = (bb.get() & 0xFF) / 255f;
        hc.showTimeNs = bb.getLong();
        highlightQueue.add(hc);
        ByteBuffer resp = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN);
        resp.putLong(hc.showTimeNs);
        sendResp(out, RSP_HIGHLIGHT_ACK, resp.array());
    }

    private void handleClearHighlights(OutputStream out, byte[] payload) throws IOException {
        ByteBuffer bb = ByteBuffer.wrap(payload).order(ByteOrder.LITTLE_ENDIAN);
        HighlightCmd hc = new HighlightCmd();
        hc.isClear = true;
        hc.showTimeNs = bb.getLong();
        highlightQueue.add(hc);
        ByteBuffer resp = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN);
        resp.putLong(hc.showTimeNs);
        sendResp(out, RSP_HIGHLIGHT_ACK, resp.array());
    }

    private void awaitAndForward(OutputStream out, int rsp, int sec) throws IOException {
        try {
            byte[] data = responseQueue.poll(sec, TimeUnit.SECONDS);
            if (data != null) sendResp(out, rsp, data);
            else sendResp(out, RSP_ERROR, "timeout".getBytes());
        } catch (InterruptedException e) {
            sendResp(out, RSP_ERROR, "interrupted".getBytes());
        }
    }

    // ── IO helpers ──

    private int readIntLE(InputStream in) throws IOException {
        byte[] b = readExact(in, 4);
        return (b[0]&0xFF)|((b[1]&0xFF)<<8)|((b[2]&0xFF)<<16)|((b[3]&0xFF)<<24);
    }

    private byte[] readExact(InputStream in, int n) throws IOException {
        byte[] buf = new byte[n];
        int off = 0;
        while (off < n) {
            int r = in.read(buf, off, n - off);
            if (r == -1) throw new IOException("EOF");
            off += r;
        }
        return buf;
    }

    private void sendResp(OutputStream out, int cmd, byte[] payload) throws IOException {
        byte[] hdr = new byte[5];
        hdr[0] = (byte) cmd;
        hdr[1] = (byte)  payload.length;
        hdr[2] = (byte) (payload.length >>> 8);
        hdr[3] = (byte) (payload.length >>> 16);
        hdr[4] = (byte) (payload.length >>> 24);
        synchronized (out) {
            out.write(hdr);
            out.write(payload);
            out.flush();
        }
    }

    private static class TexLoad {
        final int id, width, height;
        final byte[] rgba;
        TexLoad(int id, int w, int h, byte[] rgba) {
            this.id = id; this.width = w; this.height = h; this.rgba = rgba;
        }
    }

    static class HighlightCmd {
        boolean isClear;
        float x1, y1, x2, y2;
        float r, g, b, a;
        long showTimeNs;
    }

    // ═══════════════════════════════════ CAMERA STREAM ══

    private void startCamera() {
        camThread = new HandlerThread("cam");
        camThread.start();
        camHandler = new Handler(camThread.getLooper());
        try {
            setupEncoder();
            setupCamServer();
            openCamera();
        } catch (Exception e) {
            Log.e(TAG, "Camera start failed", e);
        }
    }

    private void setupEncoder() throws IOException {
        MediaFormat fmt = MediaFormat.createVideoFormat("video/avc", CAM_WIDTH, CAM_HEIGHT);
        fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        fmt.setInteger(MediaFormat.KEY_BIT_RATE, CAM_BITRATE);
        fmt.setInteger(MediaFormat.KEY_FRAME_RATE, CAM_FPS);
        fmt.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
        encoder = MediaCodec.createEncoderByType("video/avc");
        encoder.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        encoderInput = encoder.createInputSurface();
        encoder.start();
        new Thread(this::drainEncoder, "drain").start();
    }

    private void setupCamServer() throws IOException {
        camServerSocket = new ServerSocket(CAM_PORT);
        new Thread(() -> {
            while (running) {
                try {
                    Socket s = camServerSocket.accept();
                    s.setTcpNoDelay(true);
                    OutputStream out = s.getOutputStream();
                    if (codecConfig != null) out.write(codecConfig);
                    synchronized (this) {
                        if (camClient != null)
                            try { camClient.close(); } catch (IOException ignored) {}
                        camClient = s;
                        camOut = out;
                    }
                    Log.i(TAG, "Camera client connected");
                } catch (IOException e) {
                    if (running) Log.w(TAG, "cam accept", e);
                }
            }
        }, "cam-accept").start();
    }

    private void openCamera() throws CameraAccessException {
        CameraManager cm = (CameraManager) getSystemService(CAMERA_SERVICE);
        String frontId = null;
        for (String id : cm.getCameraIdList()) {
            Integer facing = cm.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT) {
                frontId = id;
                break;
            }
        }
        if (frontId == null)
            throw new CameraAccessException(CameraAccessException.CAMERA_ERROR, "no front camera");

        Log.i(TAG, "Opening front camera id=" + frontId);
        cm.openCamera(frontId, new CameraDevice.StateCallback() {
            @Override
            public void onOpened(CameraDevice dev) {
                cameraDevice = dev;
                try {
                    CaptureRequest.Builder req =
                        dev.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
                    req.addTarget(encoderInput);
                    req.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,
                            new android.util.Range<>(CAM_FPS, CAM_FPS));
                    dev.createCaptureSession(Collections.singletonList(encoderInput),
                        new CameraCaptureSession.StateCallback() {
                            @Override
                            public void onConfigured(CameraCaptureSession sess) {
                                captureSession = sess;
                                try {
                                    sess.setRepeatingRequest(req.build(), null, camHandler);
                                    Log.i(TAG, "Camera capturing");
                                } catch (CameraAccessException e) {
                                    Log.e(TAG, "setRepeatingRequest", e);
                                }
                            }
                            @Override
                            public void onConfigureFailed(CameraCaptureSession s) {
                                Log.e(TAG, "Camera session config failed");
                            }
                        }, camHandler);
                } catch (CameraAccessException e) {
                    Log.e(TAG, "create session", e);
                }
            }

            @Override public void onDisconnected(CameraDevice d) { d.close(); }
            @Override public void onError(CameraDevice d, int err) {
                Log.e(TAG, "camera error " + err); d.close();
            }
        }, camHandler);
    }

    private void drainEncoder() {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        while (running) {
            int idx;
            try {
                idx = encoder.dequeueOutputBuffer(info, 10_000);
            } catch (IllegalStateException e) {
                break;
            }
            if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                MediaFormat fmt = encoder.getOutputFormat();
                ByteBuffer sps = fmt.getByteBuffer("csd-0");
                ByteBuffer pps = fmt.getByteBuffer("csd-1");
                if (sps != null && pps != null) {
                    byte[] cc = new byte[sps.remaining() + pps.remaining()];
                    sps.get(cc, 0, sps.remaining());
                    pps.get(cc, sps.position(), pps.remaining());
                    codecConfig = cc;
                }
                continue;
            }
            if (idx < 0) continue;

            ByteBuffer buf = encoder.getOutputBuffer(idx);
            if (buf != null && info.size > 0) {
                buf.position(info.offset);
                buf.limit(info.offset + info.size);
                byte[] chunk = new byte[info.size];
                buf.get(chunk);

                if ((info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                    codecConfig = chunk;
                } else {
                    OutputStream out = camOut;
                    if (out != null) {
                        try {
                            out.write(chunk);
                        } catch (IOException e) {
                            Log.w(TAG, "cam write failed", e);
                            synchronized (this) {
                                try { if (camClient != null) camClient.close(); }
                                catch (IOException ignored) {}
                                camClient = null;
                                camOut = null;
                            }
                        }
                    }
                }
            }
            encoder.releaseOutputBuffer(idx, false);
        }
    }

    // ═══════════════════════════════════ GL RENDERER ══

    private class FullScreenRenderer implements GLSurfaceView.Renderer {
        // bitmap shader
        private int prog, aPos, aTC, uTex;
        private FloatBuffer posBuf, tcBuf;
        private final int[] texIds = new int[MAX_TEXTURES];
        private final boolean[] texOk = new boolean[MAX_TEXTURES];
        private int activeTex = -1;

        // highlight shader
        private int hlProg, hlAPos, hlUColor;
        private FloatBuffer hlPosBuf;
        private final ArrayList<float[]> activeHighlights = new ArrayList<>();

        // timing
        private long lastNs;
        private volatile long framePeriodNs = 16_666_667L;
        private long fpSum;
        private int fpN;

        long getFramePeriodNs() { return framePeriodNs; }

        @Override
        public void onSurfaceCreated(GL10 gl, EGLConfig cfg) {
            GLES20.glClearColor(0, 0, 0, 1);
            GLES20.glDisable(GLES20.GL_DEPTH_TEST);
            GLES20.glDisable(GLES20.GL_DITHER);

            // bitmap textured-quad program
            int vs = shader(GLES20.GL_VERTEX_SHADER,
                "attribute vec4 aPos;" +
                "attribute vec2 aTC;" +
                "varying vec2 vTC;" +
                "void main(){gl_Position=aPos;vTC=aTC;}");
            int fs = shader(GLES20.GL_FRAGMENT_SHADER,
                "precision mediump float;" +
                "varying vec2 vTC;" +
                "uniform sampler2D uTex;" +
                "void main(){gl_FragColor=texture2D(uTex,vTC);}");
            prog = GLES20.glCreateProgram();
            GLES20.glAttachShader(prog, vs);
            GLES20.glAttachShader(prog, fs);
            GLES20.glLinkProgram(prog);
            aPos = GLES20.glGetAttribLocation(prog, "aPos");
            aTC  = GLES20.glGetAttribLocation(prog, "aTC");
            uTex = GLES20.glGetUniformLocation(prog, "uTex");

            // solid-color program for highlight overlays
            int hlVs = shader(GLES20.GL_VERTEX_SHADER,
                "attribute vec4 aPos;" +
                "void main(){gl_Position=aPos;}");
            int hlFs = shader(GLES20.GL_FRAGMENT_SHADER,
                "precision mediump float;" +
                "uniform vec4 uColor;" +
                "void main(){gl_FragColor=uColor;}");
            hlProg = GLES20.glCreateProgram();
            GLES20.glAttachShader(hlProg, hlVs);
            GLES20.glAttachShader(hlProg, hlFs);
            GLES20.glLinkProgram(hlProg);
            hlAPos   = GLES20.glGetAttribLocation(hlProg, "aPos");
            hlUColor = GLES20.glGetUniformLocation(hlProg, "uColor");

            posBuf = fb(new float[]{-1,-1, 1,-1, -1,1, 1,1});
            tcBuf  = fb(new float[]{ 0, 1, 1, 1,  0,0, 1,0});
            hlPosBuf = ByteBuffer.allocateDirect(8 * 4)
                .order(ByteOrder.nativeOrder()).asFloatBuffer();

            activeTex = -1;
            activeHighlights.clear();
            for (int i = 0; i < MAX_TEXTURES; i++) texOk[i] = false;
            GLES20.glGenTextures(MAX_TEXTURES, texIds, 0);
            for (int i = 0; i < MAX_TEXTURES; i++) {
                GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, texIds[i]);
                GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,
                    GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_NEAREST);
                GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,
                    GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_NEAREST);
                GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,
                    GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE);
                GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,
                    GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE);
            }
            Log.i(TAG, "GL ready");
        }

        @Override
        public void onSurfaceChanged(GL10 gl, int w, int h) {
            GLES20.glViewport(0, 0, w, h);
            Log.i(TAG, "Viewport " + w + "x" + h);
        }

        @Override
        public void onDrawFrame(GL10 gl) {
            long now = System.nanoTime();

            if (lastNs > 0) {
                long d = now - lastNs;
                if (d > 0 && d < 100_000_000L) {
                    fpSum += d;
                    if (++fpN >= 60) {
                        framePeriodNs = fpSum / fpN;
                        fpSum = 0; fpN = 0;
                    }
                }
            }
            lastNs = now;

            // upload queued textures
            TexLoad ld;
            while ((ld = loadQueue.poll()) != null) {
                ByteBuffer px = ByteBuffer.allocateDirect(ld.rgba.length);
                px.put(ld.rgba).position(0);
                GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, texIds[ld.id]);
                GLES20.glTexImage2D(GLES20.GL_TEXTURE_2D, 0, GLES20.GL_RGBA,
                    ld.width, ld.height, 0,
                    GLES20.GL_RGBA, GLES20.GL_UNSIGNED_BYTE, px);
                texOk[ld.id] = true;
                ByteBuffer r = ByteBuffer.allocate(5).order(ByteOrder.LITTLE_ENDIAN);
                r.putInt(ld.id); r.put((byte) 1);
                responseQueue.offer(r.array());
            }

            // check pending texture swap
            int pt = pendingTexture;
            if (pt >= 0 && pt < MAX_TEXTURES && texOk[pt]) {
                long target = pendingShowTimeNs;
                long appear = now + 2 * framePeriodNs;
                if (target == 0 || appear >= target) {
                    activeTex = pt;
                    pendingTexture = -1;
                    ByteBuffer r = ByteBuffer.allocate(20).order(ByteOrder.LITTLE_ENDIAN);
                    r.putInt(pt); r.putLong(target); r.putLong(appear);
                    responseQueue.offer(r.array());
                }
            }

            // activate due highlight commands
            long appear = now + 2 * framePeriodNs;
            Iterator<HighlightCmd> it = highlightQueue.iterator();
            while (it.hasNext()) {
                HighlightCmd hc = it.next();
                if (hc.showTimeNs == 0 || appear >= hc.showTimeNs) {
                    it.remove();
                    if (hc.isClear) {
                        activeHighlights.clear();
                    } else {
                        activeHighlights.add(new float[]{
                            hc.x1, hc.y1, hc.x2, hc.y2,
                            hc.r, hc.g, hc.b, hc.a});
                    }
                }
            }

            // draw bitmap
            GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT);
            if (activeTex >= 0 && texOk[activeTex]) {
                GLES20.glUseProgram(prog);
                GLES20.glEnableVertexAttribArray(aPos);
                GLES20.glVertexAttribPointer(aPos, 2, GLES20.GL_FLOAT, false, 0, posBuf);
                GLES20.glEnableVertexAttribArray(aTC);
                GLES20.glVertexAttribPointer(aTC, 2, GLES20.GL_FLOAT, false, 0, tcBuf);
                GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
                GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, texIds[activeTex]);
                GLES20.glUniform1i(uTex, 0);
                GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
                GLES20.glDisableVertexAttribArray(aPos);
                GLES20.glDisableVertexAttribArray(aTC);
            }

            // draw highlight overlays
            if (!activeHighlights.isEmpty()) {
                GLES20.glEnable(GLES20.GL_BLEND);
                GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA);
                GLES20.glUseProgram(hlProg);
                GLES20.glEnableVertexAttribArray(hlAPos);
                for (float[] hl : activeHighlights) {
                    float l = Math.min(hl[0], hl[2]) * 2f - 1f;
                    float r2 = Math.max(hl[0], hl[2]) * 2f - 1f;
                    float t = 1f - Math.min(hl[1], hl[3]) * 2f;
                    float b = 1f - Math.max(hl[1], hl[3]) * 2f;
                    hlPosBuf.clear();
                    hlPosBuf.put(l); hlPosBuf.put(b);
                    hlPosBuf.put(r2); hlPosBuf.put(b);
                    hlPosBuf.put(l); hlPosBuf.put(t);
                    hlPosBuf.put(r2); hlPosBuf.put(t);
                    hlPosBuf.position(0);
                    GLES20.glVertexAttribPointer(hlAPos, 2, GLES20.GL_FLOAT,
                        false, 0, hlPosBuf);
                    GLES20.glUniform4f(hlUColor, hl[4], hl[5], hl[6], hl[7]);
                    GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
                }
                GLES20.glDisableVertexAttribArray(hlAPos);
                GLES20.glDisable(GLES20.GL_BLEND);
            }
        }

        private int shader(int type, String src) {
            int s = GLES20.glCreateShader(type);
            GLES20.glShaderSource(s, src);
            GLES20.glCompileShader(s);
            int[] ok = {0};
            GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, ok, 0);
            if (ok[0] == 0) {
                Log.e(TAG, "Shader: " + GLES20.glGetShaderInfoLog(s));
                GLES20.glDeleteShader(s);
            }
            return s;
        }

        private FloatBuffer fb(float[] a) {
            FloatBuffer b = ByteBuffer.allocateDirect(a.length * 4)
                .order(ByteOrder.nativeOrder()).asFloatBuffer();
            b.put(a).position(0);
            return b;
        }
    }
}
