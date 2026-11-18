"use client";
import {useEffect,useRef,useState} from "react";
import dynamic from "next/dynamic";
import {api,errorText} from "@/lib/api";
import type {BusinessSummary} from "@/lib/contracts";
import Chat from "@/features/chat";
import {Failure} from "@/components/ui/feedback";
const Conversations=dynamic(()=>import("@/features/conversations"));
const Leads=dynamic(()=>import("@/features/leads"));
const Knowledge=dynamic(()=>import("@/features/knowledge"));
type Tab="Chat"|"Conversations"|"Leads"|"Knowledge base";
const tabs:Tab[]=["Chat","Conversations","Leads","Knowledge base"];
function BusinessWorkspace({
  business,
  active,
  tab,
}: {
  business: BusinessSummary;
  active: boolean;
  tab: Tab;
}) {
  const [revision, setRevision] = useState(0);
  const visited=useRef(new Set<Tab>());
  useEffect(()=>{if(active)visited.current.add(tab);},[active,tab]);
  return (
    <div
      hidden={!active}
      data-active={active}
      data-business={business.businessId}
    >
      <div hidden={tab !== "Chat"}>
        {((active && tab === "Chat") || visited.current.has("Chat")) && <Chat
          business={business.businessId}
          businessName={business.businessName}
          role={business.agentRole}
          starterQuestions={business.starterQuestions}
          onChange={() => setRevision((r) => r + 1)}
        />}
      </div>
      <div hidden={tab !== "Conversations"}>
        {((active && tab === "Conversations") || visited.current.has("Conversations")) && <Conversations
          business={business.businessId}
          active={active && tab === "Conversations"}
          revision={revision}
        />}
      </div>
      <div hidden={tab !== "Leads"}>
        {((active && tab === "Leads") || visited.current.has("Leads")) && <Leads
          business={business.businessId}
          active={active && tab === "Leads"}
          revision={revision}
        />}
      </div>
      <div hidden={tab !== "Knowledge base"}>
        {((active && tab === "Knowledge base") || visited.current.has("Knowledge base")) && <Knowledge
          business={business.businessId}
          active={active && tab === "Knowledge base"}
        />}
      </div>
    </div>
  );
}
export default function Workspace(){
 const [businesses,setBusinesses]=useState<BusinessSummary[]>([]);
 const [selected,setSelected]=useState("");const [tab,setTab]=useState<Tab>("Chat");
 const [error,setError]=useState("");const [revision,setRevision]=useState(0);
 useEffect(()=>{const controller=new AbortController();api.businesses(controller.signal).then(result=>{if(!controller.signal.aborted){setBusinesses(result.businesses);setSelected(old=>old||result.businesses.find(b=>b.businessId==="training-institute")?.businessId||result.businesses[0]?.businessId||"");setError("");}}).catch(e=>{if(!controller.signal.aborted)setError(errorText(e));});return()=>controller.abort();},[revision]);
 return <div className="simple-workspace"><header className="workspace-topbar" id="top"><a className="wordmark" href="#top" onClick={()=>setTab("Chat")} aria-label="Conversa home">conversa<span aria-hidden="true">&#10035;</span></a><span className="workspace-label eyebrow">YOUR CUSTOMER WORKSPACE</span><label className="business-pill"><span className="status-dot" aria-hidden="true"/><span className="sr-only">Business</span><select id="business" value={selected} onChange={e=>setSelected(e.target.value)} disabled={!businesses.length}>{businesses.map(b=><option value={b.businessId} key={b.businessId}>{b.businessName}</option>)}</select></label></header>
 <nav className="section-navigation" aria-label="Workspace sections">{tabs.map((value,i)=><button key={value} aria-current={tab===value?"page":undefined} onClick={()=>setTab(value)}><span className="eyebrow">0{i+1}</span><span>{value}</span><span className="section-nav-mark" aria-hidden="true">&#10035;</span></button>)}</nav>
 <div className="workspace-body"><Failure value={error}/>{error&&<button className="pill-button" onClick={()=>setRevision(r=>r+1)}>Retry connection</button>}{!businesses.length&&!error&&<div className="skeleton" role="status">Loading your workspace...</div>}{businesses.map(b=><BusinessWorkspace key={b.businessId} business={b} active={b.businessId===selected} tab={tab}/>)}</div>
 <footer className="workspace-footer"><span>Conversa / Local workspace</span><span>Notifications are simulated</span></footer></div>;
}
