"""#242 同意书冠/桥模板推荐收敛到唯一：处置词→唯一模板,不外溢多签。
用 node vm 探针执行 consent_form.js 的 consentTemplatesForItem(纯函数,无DOM)。"""
import json
import subprocess
import unittest

PROBE = r'''
const vm=require("vm"), fs=require("fs");
const src=fs.readFileSync("local_app/static/consent_form.js","utf8");
const sb={document:{getElementById:()=>null,querySelector:()=>null,addEventListener:()=>{}},window:{},console};
vm.createContext(sb);
try{vm.runInContext(src,sb);}catch(e){}
// 复现 #242 场景:同时存在 全瓷冠修复 与 烤瓷冠、桥修复
const T=[{name:"全瓷冠修复知情同意书",category:"修复"},
         {name:"烤瓷冠、桥修复知情同意书",category:"修复"},
         {name:"贴面修复知情同意书",category:"修复"},
         {name:"活动义齿知情同意书",category:"修复"}];
const out={};
for(const it of ["全瓷冠","固定桥","冠桥","烤瓷冠"]) out[it]=sb.consentTemplatesForItem(it,T).map(x=>x.name);
console.log(JSON.stringify(out));
'''


class CrownBridgeConsentTest(unittest.TestCase):
    def test_unique_template_per_treatment(self):
        try:
            r = subprocess.run(["node", "-e", PROBE], capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            self.skipTest("node 不可用")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        # 每个处置词都收敛到恰好 1 份,且选对
        self.assertEqual(out["全瓷冠"], ["全瓷冠修复知情同意书"])
        self.assertEqual(out["固定桥"], ["烤瓷冠、桥修复知情同意书"])
        self.assertEqual(len(out["冠桥"]), 1)
        self.assertEqual(out["烤瓷冠"], ["烤瓷冠、桥修复知情同意书"])


if __name__ == "__main__":
    unittest.main()
