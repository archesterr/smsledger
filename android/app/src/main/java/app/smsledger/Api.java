package app.smsledger;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.List;

/** The server's /ingest. The device key can only add SMS; nothing is ever read back with it. */
public final class Api {
    public static final class Reply {
        public final int code;
        public final JSONObject json;

        Reply(int code, JSONObject json) {
            this.code = code;
            this.json = json;
        }

        /** Error bodies never carry "status" (views/api.py), so this means the server kept the SMS. */
        public boolean ok() {
            return code == 200 && json.has("status");
        }

        /** The Persian text the server wrote for the phone, if any. */
        public String message() {
            return json.optString("message", "");
        }
    }

    private final String base, key, agent;

    /** base: https://host/ (Core.serverBase), key: the device key from the setup page. */
    public Api(String base, String key, String agent) {
        this.base = base;
        this.key = key;
        this.agent = agent;
    }

    /** The hello after the Connect link: the server marks this phone as connected. */
    public Reply connect() throws IOException {
        return request("POST", "ingest?source=connect", new JSONObject(), true);
    }

    /** SMS with their arrival times, the same "items" batch the iPhone backup reader sends. */
    public Reply send(List<Queue.Item> items) throws IOException {
        JSONArray list = new JSONArray();
        try {
            for (Queue.Item i : items) list.put(new JSONObject().put("text", i.text).put("at", i.at));
            JSONObject body = new JSONObject().put("items", list).put("source", items.get(0).src);
            return request("POST", "ingest", body, true);
        } catch (JSONException e) {
            throw new IOException(e);
        }
    }

    /** Public settings: the Shamsi import periods and the one-time-code filter. No key needed. */
    public Reply config() throws IOException {
        return request("GET", "android/config.json", null, false);
    }

    private Reply request(String method, String path, JSONObject body, boolean auth) throws IOException {
        HttpURLConnection c = (HttpURLConnection) URI.create(base + path).toURL().openConnection();
        try {
            c.setRequestMethod(method);
            c.setConnectTimeout(15_000);
            c.setReadTimeout(60_000);
            c.setInstanceFollowRedirects(false);  // the key never follows a redirect elsewhere
            c.setRequestProperty("User-Agent", agent);
            c.setRequestProperty("Accept", "application/json");
            if (auth) c.setRequestProperty("Authorization", "Bearer " + key);
            if (body != null) {
                byte[] data = body.toString().getBytes(StandardCharsets.UTF_8);
                c.setDoOutput(true);
                c.setFixedLengthStreamingMode(data.length);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                try (OutputStream out = c.getOutputStream()) {
                    out.write(data);
                }
            }
            int code = c.getResponseCode();
            InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
            return new Reply(code, parse(in));
        } finally {
            c.disconnect();
        }
    }

    private static JSONObject parse(InputStream in) throws IOException {
        if (in == null) return new JSONObject();
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        try (InputStream s = in) {
            byte[] chunk = new byte[8192];
            for (int n = s.read(chunk); n > 0; n = s.read(chunk)) {
                if (buf.size() > 1_000_000) break;
                buf.write(chunk, 0, n);
            }
        }
        try {
            return new JSONObject(buf.toString("UTF-8"));
        } catch (JSONException e) {  // a proxy's HTML error page
            return new JSONObject();
        }
    }
}
