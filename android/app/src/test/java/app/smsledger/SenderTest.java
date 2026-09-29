package app.smsledger;

import static org.junit.Assert.assertEquals;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

/** The queue and the sender against a stand-in /ingest. */
public class SenderTest {
    @Rule public TemporaryFolder tmp = new TemporaryFolder();

    private ServerSocket server;
    private final List<JSONObject> received = new ArrayList<>();
    private final List<String> auth = new ArrayList<>();
    private int[] codes = {200};
    private int calls;
    private Queue queue;
    private Api api;

    @Before
    public void setUp() throws Exception {
        // a stand-in /ingest (the JDK's HttpServer isn't on Android's unit-test classpath)
        server = new ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"));
        Thread t = new Thread(() -> {
            while (!server.isClosed()) {
                try (Socket c = server.accept()) {
                    handle(c);
                } catch (Exception e) {
                    // closed by tearDown or a test
                }
            }
        });
        t.setDaemon(true);
        t.start();
        queue = new Queue(new File(tmp.getRoot(), "queue.jsonl"));
        api = new Api("http://127.0.0.1:" + server.getLocalPort() + "/", "sml_test", "test");
    }

    private void handle(Socket c) throws Exception {
        InputStream in = c.getInputStream();
        int length = 0;
        String authorization = null;
        for (String line = readLine(in); !line.isEmpty(); line = readLine(in)) {
            String lower = line.toLowerCase();
            if (lower.startsWith("content-length:")) length = Integer.parseInt(line.substring(15).trim());
            if (lower.startsWith("authorization:")) authorization = line.substring(14).trim();
        }
        byte[] body = new byte[length];
        for (int n = 0; n < length; ) n += in.read(body, n, length - n);
        int code = codes[Math.min(calls++, codes.length - 1)];
        if (code == 200) received.add(new JSONObject(new String(body, StandardCharsets.UTF_8)));
        auth.add(authorization);
        byte[] out = (code == 200 ? "{\"status\": \"ok\"}" : "{\"error\": \"x\"}").getBytes(StandardCharsets.UTF_8);
        OutputStream o = c.getOutputStream();
        o.write(("HTTP/1.1 " + code + " X\r\nContent-Type: application/json\r\nContent-Length: " + out.length
                + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        o.write(out);
        o.flush();
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        for (int ch = in.read(); ch != -1 && ch != '\n'; ch = in.read()) if (ch != '\r') b.write(ch);
        return b.toString("UTF-8");
    }

    @After
    public void tearDown() throws Exception {
        server.close();
    }

    private void fill(int n, String src) throws Exception {
        List<Queue.Item> items = new ArrayList<>();
        for (int i = 0; i < n; i++) items.add(new Queue.Item("مانده " + i, 1_700_000_000_000L + i, src));
        queue.add(items);
    }

    @Test
    public void sendsInBatchesAndEmptiesTheQueue() throws Exception {
        fill(250, "android-import");
        assertEquals(Sender.Outcome.DONE, Sender.drain(queue, api, null));
        assertEquals(0, queue.size());
        assertEquals(3, received.size());
        assertEquals(Sender.BATCH, received.get(0).getJSONArray("items").length());
        assertEquals("android-import", received.get(0).getString("source"));
        assertEquals(1_700_000_000_000L, received.get(0).getJSONArray("items").getJSONObject(0).getLong("at"));
        assertEquals("Bearer sml_test", auth.get(0));
    }

    @Test
    public void oneRequestCarriesOneSource() throws Exception {
        fill(2, "android");
        fill(1, "android-import");
        Sender.drain(queue, api, null);
        assertEquals(Arrays.asList("android", "android-import"),
                Arrays.asList(received.get(0).getString("source"), received.get(1).getString("source")));
    }

    @Test
    public void keepsTheQueueWhenTheServerIsDown() throws Exception {
        fill(3, "android");
        codes = new int[]{502};
        assertEquals(Sender.Outcome.RETRY, Sender.drain(queue, api, null));
        assertEquals(3, queue.size());
    }

    @Test
    public void keepsTheQueueWhenTheKeyIsRevoked() throws Exception {
        fill(3, "android");
        codes = new int[]{401};
        assertEquals(Sender.Outcome.REVOKED, Sender.drain(queue, api, null));
        assertEquals(3, queue.size());
    }

    @Test
    public void noInternetIsARetry() throws Exception {
        fill(1, "android");
        server.close();
        assertEquals(Sender.Outcome.RETRY, Sender.drain(queue, api, null));
        assertEquals(1, queue.size());
    }

    @Test
    public void tooLargeSplitsTheBatch() throws Exception {
        fill(4, "android");
        codes = new int[]{413, 200};
        assertEquals(Sender.Outcome.DONE, Sender.drain(queue, api, null));
        assertEquals(0, queue.size());
        assertEquals(2, received.get(0).getJSONArray("items").length());
    }

    @Test
    public void aTornLineIsSkipped() throws Exception {
        fill(1, "android");
        try (OutputStream o = new java.io.FileOutputStream(new File(tmp.getRoot(), "queue.jsonl"), true)) {
            o.write("{\"text\": \"half".getBytes(StandardCharsets.UTF_8));
        }
        fill(1, "android");
        assertEquals(2, queue.size());
        assertEquals(Sender.Outcome.DONE, Sender.drain(queue, api, null));
        assertEquals(2, received.get(0).getJSONArray("items").length());
    }
}
