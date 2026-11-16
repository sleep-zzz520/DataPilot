import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import WorkspaceSidebar from "./WorkspaceSidebar.vue";
const sessions = [
  { id: "s1", title: "十月销售对比", project_id: "legacy-project" },
  { id: "s2", title: "临时分析" },
];
const render = () =>
  mount(WorkspaceSidebar, {
    props: { sessions, sessionId: "s1" },
    global: { stubs: { "router-link": { template: "<a><slot /></a>" } } },
  });
describe("WorkspaceSidebar", () => {
  it("keeps all existing sessions visible without project controls", async () => {
    const w = render();
    expect(w.findAll(".session-link")).toHaveLength(2);
    expect(w.find('[aria-current="page"]').text()).toBe("十月销售对比");
    expect(w.find(".project-section").exists()).toBe(false);
    await w.findAll(".session-link")[1].trigger("click");
    expect(w.emitted("select-session")[0]).toEqual(["s2"]);
    w.unmount();
  });
  it("expands search and restores the full list on Escape", async () => {
    const w = render();
    await w.find('[aria-label="搜索会话"]').trigger("click");
    await w.find("input").setValue("十月");
    expect(w.findAll(".session-link")).toHaveLength(1);
    await w.find("input").trigger("keydown", { key: "Escape" });
    expect(w.find("input").exists()).toBe(false);
    expect(w.findAll(".session-link")).toHaveLength(2);
    w.unmount();
  });
});
