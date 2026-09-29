package app.smsledger;

import static org.junit.Assert.assertEquals;

import com.sun.net.httpserver.HttpServer;

import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
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

    private HttpServer server;
    private final List<JSONObject> received = new ArrayList<>();
    private final List<String> auth = new ArrayList<>();
    private int[] codes = {200};
    private int calls;
    private Queue queue;
    private Api api;

    @Before
    public void setUp() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/ingest", ex -> {
            JSONObject body = new JSONObject(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            int code = codes[Math.min(calls++, codes.length - 1)];
            if (code == 200) received.add(body);
            auth.add(ex.getRequestHeaders().getFirst("Authorization"));
            byte[] out = (code == 200 ? "{\"status\": \"ok\"}" : "{\"error\": \"x\"}").getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(code, out.length);
            try (OutputStream o = ex.getResponseBody()) {
                o.write(out);
            }
        });
        server.start();
        queue = new Queue(new File(tmp.getRoot(), "queue.jsonl"));
        api = new Api("http://127.0.0.1:" + server.getAddress().getPort() + "/", "sml_test", "test");
    }

    @After
    public void tearDown() {
        server.stop(0);
    }

    private void fill(int n, String src) throws IOException {
        List<Queue.Item> items = new ArrayList<>();
        for (int i = 0; i < n; i++) items.add(new Queue.Item("مانده " + i, 1_700_000_000_000L + i, src));
        queue.add(items);
    }

    @Test
    public void sendsInBatchesAndEmptiesTheQueue() throws IOException {
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
    public void oneRequestCarriesOneSource() throws IOException {
        fill(2, "android");
        fill(1, "android-import");
        Sender.drain(queue, api, null);
        assertEquals(Arrays.asList("android", "android-import"),
                Arrays.asList(received.get(0).getString("source"), received.get(1).getString("source")));
    }

    @Test
    public void keepsTheQueueWhenTheServerIsDown() throws IOException {
        fill(3, "android");
        codes = new int[]{502};
        assertEquals(Sender.Outcome.RETRY, Sender.drain(queue, api, null));
        assertEquals(3, queue.size());
    }

    @Test
    public void keepsTheQueueWhenTheKeyIsRevoked() throws IOException {
        fill(3, "android");
        codes = new int[]{401};
        assertEquals(Sender.Outcome.REVOKED, Sender.drain(queue, api, null));
        assertEquals(3, queue.size());
    }

    @Test
    public void noInternetIsARetry() throws IOException {
        fill(1, "android");
        server.stop(0);
        assertEquals(Sender.Outcome.RETRY, Sender.drain(queue, api, null));
        assertEquals(1, queue.size());
    }

    @Test
    public void tooLargeSplitsTheBatch() throws IOException {
        fill(4, "android");
        codes = new int[]{413, 200};
        assertEquals(Sender.Outcome.DONE, Sender.drain(queue, api, null));
        assertEquals(0, queue.size());
        assertEquals(2, received.get(0).getJSONArray("items").length());
    }

    @Test
    public void aTornLineIsSkipped() throws IOException {
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
