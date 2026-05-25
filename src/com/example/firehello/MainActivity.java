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
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.view.Surface;
import android.view.WindowManager;
import android.widget.TextView;

import java.io.IOException;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.util.Collections;
import java.util.concurrent.atomic.AtomicLong;

public class MainActivity extends Activity {
    private static final String TAG = "FireHello";
    private static final int CAM_PERM = 1;
    private static final int WIDTH = 640;
    private static final int HEIGHT = 480;
    private static final int FPS = 30;
    private static final int BITRATE = 2_000_000;
    private static final int PORT = 7777;

    private TextView status;
    private HandlerThread camThread;
    private Handler camHandler;
    private CameraDevice cameraDevice;
    private CameraCaptureSession captureSession;
    private MediaCodec encoder;
    private Surface encoderInput;
    private ServerSocket serverSocket;
    private volatile Socket clientSocket;
    private volatile OutputStream clientOut;
    private Thread drainThread;
    private Thread acceptThread;
    private byte[] codecConfig;
    private volatile boolean running = false;

    private final AtomicLong frameCount = new AtomicLong();
    private final AtomicLong bytesOut = new AtomicLong();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(R.layout.main);
        status = findViewById(R.id.tap_count);
        status.setText("requesting camera permission…");

        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, CAM_PERM);
        } else {
            startPipeline();
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] grants) {
        if (code == CAM_PERM && grants.length > 0 && grants[0] == PackageManager.PERMISSION_GRANTED) {
            startPipeline();
        } else {
            status.setText("camera permission denied");
        }
    }

    private void startPipeline() {
        camThread = new HandlerThread("cam");
        camThread.start();
        camHandler = new Handler(camThread.getLooper());
        try {
            setupEncoder();
            setupServer();
            openCamera();
            status.setText("starting…");
        } catch (Exception e) {
            Log.e(TAG, "startPipeline failed", e);
            status.setText("error: " + e.getMessage());
        }
    }

    private void setupEncoder() throws IOException {
        MediaFormat format = MediaFormat.createVideoFormat("video/avc", WIDTH, HEIGHT);
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        format.setInteger(MediaFormat.KEY_BIT_RATE, BITRATE);
        format.setInteger(MediaFormat.KEY_FRAME_RATE, FPS);
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
        encoder = MediaCodec.createEncoderByType("video/avc");
        encoder.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        encoderInput = encoder.createInputSurface();
        encoder.start();
        running = true;
        drainThread = new Thread(this::drainEncoder, "drain");
        drainThread.start();
    }

    private void setupServer() throws IOException {
        serverSocket = new ServerSocket(PORT);
        acceptThread = new Thread(() -> {
            while (running) {
                try {
                    Socket s = serverSocket.accept();
                    s.setTcpNoDelay(true);
                    OutputStream out = s.getOutputStream();
                    if (codecConfig != null) {
                        out.write(codecConfig);
                    }
                    synchronized (this) {
                        if (clientSocket != null) {
                            try { clientSocket.close(); } catch (IOException ignored) {}
                        }
                        clientSocket = s;
                        clientOut = out;
                    }
                    Log.i(TAG, "client connected from " + s.getRemoteSocketAddress());
                    runOnUiThread(() -> status.setText("client connected"));
                } catch (IOException e) {
                    if (running) Log.w(TAG, "accept", e);
                }
            }
        }, "accept");
        acceptThread.start();
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
        if (frontId == null) {
            throw new CameraAccessException(CameraAccessException.CAMERA_ERROR, "no front camera");
        }
        Log.i(TAG, "opening front camera id=" + frontId);

        cm.openCamera(frontId, new CameraDevice.StateCallback() {
            @Override
            public void onOpened(CameraDevice device) {
                cameraDevice = device;
                try {
                    CaptureRequest.Builder req = device.createCaptureRequest(
                            CameraDevice.TEMPLATE_RECORD);
                    req.addTarget(encoderInput);
                    req.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,
                            new android.util.Range<>(FPS, FPS));
                    device.createCaptureSession(
                            Collections.singletonList(encoderInput),
                            new CameraCaptureSession.StateCallback() {
                                @Override
                                public void onConfigured(CameraCaptureSession session) {
                                    captureSession = session;
                                    try {
                                        session.setRepeatingRequest(req.build(), null, camHandler);
                                        runOnUiThread(() -> status.setText(
                                                "capturing — connect player on PC"));
                                    } catch (CameraAccessException e) {
                                        Log.e(TAG, "setRepeatingRequest", e);
                                    }
                                }

                                @Override
                                public void onConfigureFailed(CameraCaptureSession s) {
                                    Log.e(TAG, "session config failed");
                                }
                            }, camHandler);
                } catch (CameraAccessException e) {
                    Log.e(TAG, "create session", e);
                }
            }

            @Override
            public void onDisconnected(CameraDevice device) {
                device.close();
            }

            @Override
            public void onError(CameraDevice device, int err) {
                Log.e(TAG, "camera error " + err);
                device.close();
            }
        }, camHandler);
    }

    private void drainEncoder() {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        long lastStatus = 0;
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
                    Log.i(TAG, "got codec config, " + cc.length + " bytes");
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
                    OutputStream out = clientOut;
                    if (out != null) {
                        try {
                            out.write(chunk);
                            bytesOut.addAndGet(chunk.length);
                            frameCount.incrementAndGet();
                        } catch (IOException e) {
                            Log.w(TAG, "client write failed, dropping", e);
                            synchronized (this) {
                                try { if (clientSocket != null) clientSocket.close(); } catch (IOException ignored) {}
                                clientSocket = null;
                                clientOut = null;
                            }
                            runOnUiThread(() -> status.setText("client disconnected"));
                        }
                    }
                }
            }
            encoder.releaseOutputBuffer(idx, false);

            long now = System.currentTimeMillis();
            if (now - lastStatus > 1000) {
                lastStatus = now;
                final long f = frameCount.get();
                final long b = bytesOut.get();
                final boolean connected = clientOut != null;
                runOnUiThread(() -> status.setText(
                        (connected ? "streaming" : "waiting for player") +
                        " — frames " + f + ", " + (b / 1024) + " KB"));
            }
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        running = false;
        try { if (captureSession != null) captureSession.close(); } catch (Exception ignored) {}
        try { if (cameraDevice != null) cameraDevice.close(); } catch (Exception ignored) {}
        try { if (encoder != null) { encoder.stop(); encoder.release(); } } catch (Exception ignored) {}
        try { if (serverSocket != null) serverSocket.close(); } catch (IOException ignored) {}
        try { if (clientSocket != null) clientSocket.close(); } catch (IOException ignored) {}
        if (camThread != null) camThread.quitSafely();
    }
}
