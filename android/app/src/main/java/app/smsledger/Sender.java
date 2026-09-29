package app.smsledger;

import java.io.IOException;
import java.util.List;

/** Empties the queue in batches. An SMS leaves the queue only once the server has kept it. */
public final class Sender {
    public enum Outcome { DONE, RETRY, REVOKED }

    public interface Progress {
        void sent(int count, int left);
    }

    static final int BATCH = 100;  // 100 × 2000 chars stays well under the server's 1 MB body limit

    private Sender() {}

    /** One drain at a time (job, screen, import): two would send and drop the same batch twice. */
    public static synchronized Outcome drain(Queue queue, Api api, Progress progress) {
        int batch = BATCH;
        try {
            while (true) {
                List<Queue.Item> items = queue.peek(batch);
                if (items.isEmpty()) return Outcome.DONE;
                Api.Reply r = api.send(items);
                if (r.ok()) {
                    queue.drop(items.size());
                    if (progress != null) progress.sent(items.size(), queue.size());
                    batch = BATCH;
                } else if (r.code == 401) {
                    return Outcome.REVOKED;  // kept: after reconnecting they go with the new key
                } else if (r.code == 413 && items.size() > 1) {
                    batch = items.size() / 2;
                } else if (r.code == 400 || r.code == 413) {
                    queue.drop(items.size());  // the server will never take these; don't block the rest
                } else {
                    return Outcome.RETRY;  // 429, 5xx, a CDN error page: try again later
                }
            }
        } catch (IOException e) {
            return Outcome.RETRY;  // no internet
        }
    }
}
