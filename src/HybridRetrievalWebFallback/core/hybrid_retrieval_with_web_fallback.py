from dotenv import load_dotenv
from langchain_google_vertexai import VertexAIEmbeddings
from langchain_community.vectorstores import Chroma
from langchain.chat_models import init_chat_model
from langchain_tavily import TavilySearch
from typing_extensions import TypedDict, List, Optional
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.vectorstores import VectorStoreRetriever
from langgraph.graph import StateGraph, END


load_dotenv()

# For Scoute/Retriver node
embeddings = VertexAIEmbeddings(model_name="text-embedding-005")
vector_store  = Chroma(persist_directory="./store/vector_store", embedding_function=embeddings)
retriver = vector_store.as_retriever()

# LLM for Supervisor node
llm = init_chat_model("gemini-2.5-flash", model_provider="google_vertexai")

# Tavily for Web search node
tavily_tool = TavilySearch(
    max_results=5,
    topic="general"
)

class State(TypedDict):
    question : str
    docs : List[dict]
    need_browse : bool
    draft: Optional[str]
    citations: List[str]
    web_snippets: List[str]
    avg_grade: float

## Format the output documents for the prompt
def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

# Let's create retriver node which does the retrival before going to the supervisor node

def retrieve(state : State)->State:
     #get the list of Document object from vector store
    documents = vector_store.similarity_search(state["question"], k= 5)
    context = format_docs(documents)
    state["draft"] = context
    # build a single context string for the supervisor prompt
    docs_as_dicts = [
        {"page_content": doc.page_content, "metadata": dict(doc.metadata or {})}
        for doc in documents
    ]
    state["docs"] = docs_as_dicts 
    return state

GRADE_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a strict relevance grader for Retrieval"
        #"say 'keep', if the chunk is direcly useful to answer the question; otherwise 'drop'. Be conservative"
         "Rate how relevant this passage is to the query from 0 to 1."
    ),
    (
        "human",
        "Question: {question} \n\nChunk: \n {chunk}\n"
    )
])

def grade_retrieved_docs(state):
    import re
    docs = state.get("docs", [])
    query = state.get("question", "")

    if not docs:
        state["docs"] = []
        state["avg_grade"] = 0.0
        return state

    graded_docs = []
    for doc in docs:
        try:
            raw = (GRADE_PROMPT | llm | StrOutputParser()).invoke(
                {"question": query, "chunk": doc["page_content"]}
            )
            raw_s = (raw or "").strip().lower()
            try:
                score = float(raw_s)
            except Exception:
                m = re.search(r"(0(?:\.\d+)?|1(?:\.0+)?)", raw_s)
                score = float(m.group(1)) if m else 0.0
        except Exception:
            score = 0.0

        graded_docs.append((doc, score))

    relevant_docs = [d for d, s in graded_docs if s >= 0.7]
    state["docs"] = relevant_docs
    state["avg_grade"] = sum(s for _, s in graded_docs) / len(graded_docs) if graded_docs else 0.0
    return state

def route_node(state):
    # write routing decision into state and return the state dict
    docs = state.get("docs", [])
    avg = float(state.get("avg_grade", 0.0))
    state["route_to"] = "web_search" if (not docs or avg < 0.6) else "answer"
    return state

def web_search(state: State) -> State:
    snippets = []
    response = tavily_tool.invoke({"query": state["question"]})
    for item in response.get("results", []):
        title = item.get('title', '')
        url = item.get('url', '')
        content = (item.get('content', ''))[:600]
        snippets.append(f"{title}\n{url}\n{content}")
    state['web_snippets'] = snippets
    return state


ANSWER_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system", 
        "You are an expert who answers only using the provided context"
        "If the context is thin, explain limits and ask a targetted follow-up"
    ),
    (
        "human",
        "User question: \n{question}\n\n"
        "Local context (top chunks):\n{docs}\n\n"
        "Web context (optional): \n{web}\n"
    )
])

def answer(state: State) -> State:
    docs_txt = "\n\n-----\n\n".join([document['page_content'] for document in state["docs"]])
    #docs_txt = format_docs(state["docs"])
    snippets = state.get('web_snippets', [])
    if len(snippets) > 0:
        webs_txt = "\n\n-----\n\n".join(state["web_snippets"]) if state["web_snippets"] else "N/A"
    else:
        webs_txt = "N/A"
    draft = (ANSWER_PROMPT | llm | StrOutputParser()).invoke({
        "question": state["question"],
        "docs": docs_txt,
        "web": webs_txt
    })
    state["draft"] = draft
    return state

builder = StateGraph(State)

builder.add_node("retrieve", retrieve)
builder.add_node("grade", grade_retrieved_docs)
builder.add_node("route", route_node)
builder.add_node("web_search", web_search)
builder.add_node("answer", answer)

builder.set_entry_point("retrieve")
builder.add_edge("retrieve", "grade")
builder.add_edge("grade", "route")
builder.add_conditional_edges("route", lambda s: s.get("route_to"), {
    "web_search": "web_search",
    "answer": "answer",
})
builder.add_edge("web_search", "answer")
builder.add_edge("answer", END)

graph = builder.compile()

if __name__ == "__main__":
    pass
