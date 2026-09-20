// Java fixture: Spring @GetMapping route + @Autowired service injection.
package fixtures;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class ItemsController {

    @Autowired
    private ItemService itemService;

    public static String javaHelper() {
        return "ok";
    }

    public static void javaWork() {
    }

    private void runCallback(Runnable cb) {
        cb.run();
    }

    @GetMapping("/api/items")
    public String listItems() {
        itemService.serve();
        Runnable r = ItemsController::javaWork;
        r.run();
        runCallback(ItemsController::javaWork);
        return javaHelper();
    }
}
