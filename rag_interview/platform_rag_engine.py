#!/usr/bin/env python3
"""
Platform FAQ RAG Engine
Handles SemiconLabs platform-related questions (dashboard, plans, certificates, etc.)
This engine is searched alongside domain-specific engines for every query.
"""
import os
import pickle
from pathlib import Path
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PLATFORM_CORPUS_DIR = Path(__file__).parent / "platform_corpus"
PLATFORM_INDEX_PATH = Path(__file__).parent / "data" / "platform_index.pkl"

class PlatformRAGEngine:
    def __init__(self):
        self.chunks = []
        self.embeddings = []
        self.topics = []

        # Build index if needed
        if self._should_rebuild():
            print("Building platform FAQ index...")
            self._build_index()
        else:
            print("Loading cached platform FAQ index...")
            self._load_index()

        print(f"Platform FAQ Engine ready: {len(self.chunks)} chunks, {len(self.topics)} topics")

    def _should_rebuild(self):
        """Check if we need to rebuild the index"""
        if not PLATFORM_INDEX_PATH.exists():
            return True

        # Check if any source file is newer than index
        index_mtime = PLATFORM_INDEX_PATH.stat().st_mtime
        for html_file in PLATFORM_CORPUS_DIR.glob("*.html"):
            if html_file.stat().st_mtime > index_mtime:
                return True

        return False

    def _parse_html(self, html_path):
        """Parse HTML file and extract Q&A chunks"""
        chunks = []

        with open(html_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')

        current_category = None

        for elem in soup.find_all(['h1', 'h2', 'p']):
            if elem.name == 'h1':
                current_category = elem.get_text(strip=True)
            elif elem.name == 'h2':
                question = elem.get_text(strip=True)
                # Get the answer (next <p> sibling)
                answer_elem = elem.find_next_sibling('p')
                if answer_elem:
                    answer = answer_elem.get_text(strip=True)
                    chunks.append({
                        'category': current_category,
                        'question': question,
                        'answer': answer,
                        'text': f"Category: {current_category}\n\nQuestion: {question}\n\nAnswer: {answer}"
                    })

        return chunks

    def _build_index(self):
        """Build vector index from platform corpus"""
        self.chunks = []
        self.topics = set()

        # Parse all HTML files
        for html_file in PLATFORM_CORPUS_DIR.glob("*.html"):
            print(f"  Parsing {html_file.name}...")
            file_chunks = self._parse_html(html_file)
            self.chunks.extend(file_chunks)
            for chunk in file_chunks:
                self.topics.add(chunk['category'])

        self.topics = sorted(list(self.topics))

        # Generate embeddings
        print(f"  Generating embeddings for {len(self.chunks)} chunks...")
        texts = [chunk['text'] for chunk in self.chunks]

        batch_size = 100
        self.embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = client.embeddings.create(
                model="text-embedding-3-small",
                input=batch
            )
            self.embeddings.extend([item.embedding for item in response.data])

        # Save index
        PLATFORM_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PLATFORM_INDEX_PATH, 'wb') as f:
            pickle.dump({
                'chunks': self.chunks,
                'embeddings': self.embeddings,
                'topics': self.topics
            }, f)

        print(f"  Saved platform index: {len(self.chunks)} chunks")

    def _load_index(self):
        """Load cached index"""
        with open(PLATFORM_INDEX_PATH, 'rb') as f:
            data = pickle.load(f)
            self.chunks = data['chunks']
            self.embeddings = data['embeddings']
            self.topics = data['topics']

    def search(self, query, k=3):
        """Search for relevant platform FAQs"""
        # Generate query embedding
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input=query
        )
        query_emb = response.data[0].embedding

        # Compute cosine similarity
        import numpy as np
        query_vec = np.array(query_emb)

        similarities = []
        for i, emb in enumerate(self.embeddings):
            emb_vec = np.array(emb)
            sim = np.dot(query_vec, emb_vec) / (np.linalg.norm(query_vec) * np.linalg.norm(emb_vec))
            similarities.append((i, sim))

        # Sort by similarity
        similarities.sort(key=lambda x: x[1], reverse=True)

        # Return top-k
        results = []
        for idx, score in similarities[:k]:
            chunk = self.chunks[idx]
            results.append({
                'category': chunk['category'],
                'question': chunk['question'],
                'answer': chunk['answer'],
                'score': float(score)
            })

        return results


if __name__ == '__main__':
    # Test the engine
    engine = PlatformRAGEngine()

    print("\n" + "="*70)
    print("PLATFORM FAQ RAG - TEST QUERIES")
    print("="*70)

    test_queries = [
        "How do I download my certificate?",
        "What is the difference between Basic and Pro plans?",
        "How do I reset my password?",
        "How long does lab provisioning take?",
        "Can I switch my EDA tool?",
    ]

    for query in test_queries:
        print(f"\nQuery: {query}")
        results = engine.search(query, k=2)
        for i, result in enumerate(results, 1):
            print(f"  [{i}] Category: {result['category']}")
            print(f"      Q: {result['question']}")
            print(f"      A: {result['answer'][:100]}...")
            print(f"      Score: {result['score']:.3f}")
