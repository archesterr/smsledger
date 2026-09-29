package app.smsledger;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * SMS waiting to be sent, one JSON object per line: {"text", "at" (ms), "src"}. An SMS is written
 * here first and removed only after the server has answered, so no signal or a reboot loses nothing.
 */
public final class Queue {
    public static final class Item {
        public final String text, src;
        public final long at;

        public Item(String text, long at, String src) {
            this.text = text;
            this.at = at;
            this.src = src;
        }
    }

    private static final Object LOCK = new Object();  // the receiver, the job and the import share one file
    private final File file;

    public Queue(File file) {
        this.file = file;
    }

    public void add(List<Item> items) throws IOException {
        if (items.isEmpty()) return;
        StringBuilder s = new StringBuilder();
        for (Item i : items) {
            try {
                s.append(new JSONObject().put("text", i.text).put("at", i.at).put("src", i.src)).append('\n');
            } catch (JSONException e) {
                throw new IOException(e);
            }
        }
        synchronized (LOCK) {
            if (endsTorn()) s.insert(0, '\n');  // a crash cut the last line short: keep it off ours
            try (OutputStream out = new FileOutputStream(file, true)) {
                out.write(s.toString().getBytes(StandardCharsets.UTF_8));
            }
        }
    }

    public void add(Item item) throws IOException {
        add(Collections.singletonList(item));
    }

    /** Up to max items from the front that share one source (one request carries one source). */
    public List<Item> peek(int max) throws IOException {
        List<Item> out = new ArrayList<>();
        synchronized (LOCK) {
            for (String line : lines()) {
                Item i = parse(line);
                if (i == null) continue;
                if (out.size() == max || (!out.isEmpty() && !out.get(0).src.equals(i.src))) break;
                out.add(i);
            }
        }
        return out;
    }

    /** Removes the first n items (the ones peek returned and the server accepted). */
    public void drop(int n) throws IOException {
        synchronized (LOCK) {
            List<String> rest = new ArrayList<>();
            int skipped = 0;
            for (String line : lines()) {
                if (parse(line) == null) continue;  // a torn line from a crash mid-write
                if (skipped < n) {
                    skipped++;
                } else {
                    rest.add(line);
                }
            }
            File tmp = new File(file.getPath() + ".tmp");
            try (OutputStream out = new FileOutputStream(tmp)) {
                for (String line : rest) out.write((line + "\n").getBytes(StandardCharsets.UTF_8));
            }
            if (!tmp.renameTo(file)) throw new IOException("could not replace " + file);
        }
    }

    public int size() {
        synchronized (LOCK) {
            try {
                int n = 0;
                for (String line : lines()) if (parse(line) != null) n++;
                return n;
            } catch (IOException e) {
                return 0;
            }
        }
    }

    private boolean endsTorn() throws IOException {
        if (!file.exists() || file.length() == 0) return false;
        try (RandomAccessFile f = new RandomAccessFile(file, "r")) {
            f.seek(f.length() - 1);
            return f.read() != '\n';
        }
    }

    private List<String> lines() throws IOException {
        List<String> out = new ArrayList<>();
        if (!file.exists()) return out;
        try (BufferedReader r = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8))) {
            for (String line = r.readLine(); line != null; line = r.readLine()) out.add(line);
        }
        return out;
    }

    private static Item parse(String line) {
        try {
            JSONObject o = new JSONObject(line);
            return new Item(o.getString("text"), o.getLong("at"), o.optString("src", "android"));
        } catch (JSONException e) {
            return null;
        }
    }
}
