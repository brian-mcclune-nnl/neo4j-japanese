"""Parser for the JMdict_e_exampl.xml file."""

import argparse
import contextlib
import itertools
import logging
import sys
import textwrap
import datetime

from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from lxml import etree
from neo4j import GraphDatabase, Session, Transaction


logger = logging.getLogger('jmdict')

DOT = '\xb7'
KANA_DOT = '\u30fb'

HIRAGANA = ('\u3040', '\u309f')
KATAKANA = ('\u30a0', '\u30ff')
KATAKANA_PHONETIC_EXT = ('\u31f0', '\u31ff')


def is_kana(string: str) -> bool:
    """Returns ``True`` iff every character of `string` is a kana.

    Args:
        string: The string to assess.

    Returns:
        ``True`` if the string is all kana, ``False`` otherwise.
    """

    for c in string:
        result = False
        for lo, hi in (HIRAGANA, KATAKANA, KATAKANA_PHONETIC_EXT):
            result = result or lo <= c <= hi
        if not result:
            return result

    return True


def parse_xref(xref: str) -> Dict[str, Union[str, int]]:
    """Parses `xref` into a `keb`, `reb`, and `sense` rank.

    Args:
        xref: The xref to parse.

    Returns:
        Dictionary with as many as 3 keys:
            - `keb`: The kanji cross-reference.
            - `reb`: The reading cross-reference.
            - `sense`: The sense rank number to cross-reference.
    """

    result = {}
    for token in xref.split(KANA_DOT):
        if token.isdigit():
            result['sense'] = int(token)
        elif is_kana(token):
            result['reb'] = token
        else:
            result['keb'] = token

    return result


class NeoApp:
    """Neo4j graph database application.

    Args:
        uri: The URI for the driver connection.
        user: The username for authentication.
        password: The password for authentication.
    """

    def __init__(self, uri: str, user: str, password: str):
        """Constructor."""

        self.uri = uri
        self.user = user
        logger.debug('Initializing driver, URI: %s', uri)
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

        self.closed = False

    def close(self):
        """Closes the driver connection."""

        if not self.closed:
            logger.debug('Closing driver')
            self.closed = True
            self.driver.close()

    def __del__(self):
        """Destructor."""

        self.close()

    def create_entry_constraint(self, session: Optional[Session] = None):
        """Creates a uniqueness constraint on ``ent_seq`` for Entry nodes."""

        cypher = textwrap.dedent("""\
            CREATE CONSTRAINT entry_ent_seq IF NOT EXISTS ON (n:Entry)
            ASSERT n.ent_seq IS UNIQUE
        """)
        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            session.run(cypher)
            logger.debug(
                'Added uniqueness constraint for ent_seq on Entry nodes',
            )

    def create_lsource_constraint(self, session: Optional[Session] = None):
        """Creates a uniqueness constraint on ``lang`` for Language nodes."""

        cypher = textwrap.dedent("""\
            CREATE CONSTRAINT lsource_lang IF NOT EXISTS ON (n:Language)
            ASSERT n.lang IS UNIQUE
        """)
        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            session.run(cypher)
            logger.debug(
                'Added uniqueness constraint for lang on Language nodes',
            )

    def create_example_constraint(self, session: Optional[Session] = None):
        """Creates a uniqueness constraint on ``tat`` for Example nodes."""

        cypher = textwrap.dedent("""\
            CREATE CONSTRAINT example_tat IF NOT EXISTS ON (n:Example)
            ASSERT n.tat IS UNIQUE
        """)
        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            session.run(cypher)
            logger.debug(
                'Added uniqueness constraint for tat on Example nodes',
            )

    def create_kanji_index(self, session: Optional[Session] = None):
        """Creates an single-property index for Kanji on ``keb``."""

        cypher = textwrap.dedent("""\
            CREATE INDEX kanji_keb IF NOT EXISTS FOR (n:Kanji) ON (n.keb)
        """)
        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            session.run(cypher)
            logger.debug(
                'Added index for Kanji nodes on keb',
            )

    def create_reading_index(self, session: Optional[Session] = None):
        """Creates an single-property index for Reading on ``reb``."""

        cypher = textwrap.dedent("""\
            CREATE INDEX reading_reb IF NOT EXISTS FOR (n:Reading) ON (n.reb)
        """)
        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            session.run(cypher)
            logger.debug(
                'Added index for Reading nodes on reb',
            )

    def create_sense_index(self, session: Optional[Session] = None):
        """Creates a composite-property index for Sense on (``ent_seq, rank``).
        """

        cypher = textwrap.dedent("""\
            CREATE INDEX sense_ent_seq_rank IF NOT EXISTS FOR (n:Sense)
            ON (n.ent_seq, n.rank)
        """)
        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            session.run(cypher)
            logger.debug(
                'Added composite index for Sense nodes on (ent_seq, rank)',
            )

    def add_entry(
        self,
        entry: etree.Element,
        session: Optional[Session] = None,
    ) -> int:
        """Adds an `entry` to the database.

        Args:
            entry: The entry element.
            session: A driver session for the work.
        """

        # Get the ID (ent_seq)
        ent_seq = int(entry.find('ent_seq').text)

        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            node_id = session.write_transaction(
                self._merge_and_return_entry,
                ent_seq,
            )
            logger.debug(
                'Added entry with ent_seq %s, ID: %s',
                ent_seq,
                node_id,
            )
            return node_id

    @staticmethod
    def _merge_and_return_entry(tx: Transaction, ent_seq: int) -> int:
        """Merges and returns entry `ent_seq` in the database."""

        # Add a node for the entry
        cypher = "MERGE (n:Entry {ent_seq: $ent_seq}) RETURN id(n) AS node_id"
        result = tx.run(cypher, ent_seq=ent_seq)
        record = result.single()
        return record['node_id']

    def add_kanji_for_entry(
        self,
        kanji: etree.Element,
        entry: etree.Element,
        session: Optional[Session] = None,
    ) -> int:
        """Adds `kanji` for `entry` to the database.

        Args:
            kanji: The k_ele kanji element.
            entry: The entry element.
            session: A driver session for the work.
        """

        # Get the word or phrase (keb)
        keb = kanji.find('keb').text

        # Gather the information and priority codes
        ke_infs = [elem.text for elem in kanji.findall('ke_inf')]
        ke_pris = [elem.text for elem in kanji.findall('ke_pri')]

        # Get the ent_seq of the containing entry
        ent_seq = int(entry.find('ent_seq').text)

        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            node_id = session.write_transaction(
                self._merge_and_return_kanji,
                ent_seq,
                keb,
                ke_infs,
                ke_pris,
            )
            logger.debug(
                'Added kanji %s to entry %s, ID: %s',
                keb,
                ent_seq,
                node_id,
            )
            return node_id

    @staticmethod
    def _merge_and_return_kanji(
        tx: Transaction,
        ent_seq: int,
        keb: str,
        ke_infs: List[str],
        ke_pris: List[str],
    ) -> int:
        """Merges and returns kanji `keb` related to entry `ent_seq`."""

        # Add a node for the entry
        cypher = textwrap.dedent("""\
            MATCH (e:Entry {ent_seq: $ent_seq})
            MERGE (e)-[:CONTAINS]->(k:Kanji {keb: $keb})
            ON CREATE
              SET k.ke_inf = $ke_infs
              SET k.ke_pri = $ke_pris
            RETURN id(k) AS node_id
        """)
        result = tx.run(
            cypher,
            ent_seq=ent_seq,
            keb=keb,
            ke_infs=ke_infs,
            ke_pris=ke_pris,
        )
        record = result.single()
        return record['node_id']

    def add_reading_for_entry(
        self,
        reading: etree.Element,
        entry: etree.Element,
        session: Optional[Session] = None,
    ) -> int:
        """Adds a `reading` for `entry` to the database.

        Args:
            reading: The r_ele reading element.
            entry: The entry element.
            session: A driver session for the work.
        """

        # Get the word or phrase (reb)
        reb = reading.find('reb').text

        # Gather the information and priority codes
        re_infs = [elem.text for elem in reading.findall('re_inf')]
        re_pris = [elem.text for elem in reading.findall('re_pri')]

        # Get whether a true reading for the entry
        re_nokanji = reading.find('re_nokanji') is not None

        # Get kebs that reading applies to (all if None)
        re_restr = reading.find('re_restr')
        if re_restr is not None:
            re_restr = re_restr.text

        # Get the ent_seq of the containing entry
        ent_seq = int(entry.find('ent_seq').text)

        with contextlib.ExitStack() as stack:
            session = session or stack.enter_context(self.driver.session())
            node_id = session.write_transaction(
                self._merge_and_return_reading,
                ent_seq,
                reb,
                re_nokanji,
                re_infs,
                re_pris,
            )
            kanji = session.write_transaction(
                self._merge_kanji_reading_relationships,
                ent_seq,
                reb,
                re_restr,
            )
            logger.debug(
                'Added reading %s to entry %s, (ID, kanji): %s, %s',
                reb,
                ent_seq,
                node_id,
                kanji,
            )
            return node_id

    @staticmethod
    def _merge_and_return_reading(
        tx: Transaction,
        ent_seq: int,
        reb: str,
        re_nokanji: bool,
        re_infs: List[str],
        re_pris: List[str],
    ) -> int:
        """Merges and returns reading `reb` related to entry `ent_seq`."""

        # Add a node for the entry
        cypher = textwrap.dedent("""\
            MATCH (e:Entry {ent_seq: $ent_seq})
            MERGE (e)-[:CONTAINS]->(r:Reading {reb: $reb})
            ON CREATE
              SET r.re_inf = $re_infs
              SET r.re_pri = $re_pris
              SET r.re_nokanji = $re_nokanji
            RETURN id(r) AS node_id
        """)
        result = tx.run(
            cypher,
            ent_seq=ent_seq,
            reb=reb,
            re_nokanji=re_nokanji,
            re_infs=re_infs,
            re_pris=re_pris,
        )
        record = result.single()
        return record['node_id']

    @staticmethod
    def _merge_kanji_reading_relationships(
        tx: Transaction,
        ent_seq: int,
        reb: str,
        re_restr: Union[str, None],
    ) -> List[int]:
        """Merges kanji relationships for `reb` related to entry `ent_seq`."""

        # Add kanji reading relationships
        cypher = textwrap.dedent("""\
            MATCH (e:Entry {ent_seq: $ent_seq})
            OPTIONAL MATCH (e)-[:CONTAINS]->(k:Kanji)
            WITH e, k
            WHERE k IS NOT NULL AND k.keb = coalesce($re_restr, k.keb)
            MATCH (n:Reading {reb: $reb})<-[:CONTAINS]-(e)
            MERGE (k)-[r:HAS_READING]->(n)
            RETURN id(k) AS node_id
        """)
        result = tx.run(
            cypher,
            ent_seq=ent_seq,
            reb=reb,
            re_restr=re_restr,
        )
        values = [record.value() for record in result]
        return values

    def add_sense_for_entry(
        self,
        idx: int,
        sense: etree.Element,
        entry: etree.Element,
        session: Optional[Session] = None,
    ) -> int:
        """Adds a `sense` for `entry` to the database.

        Args:
            idx: Index of sense in parent entry element.
            sense: The sense element.
            entry: The entry element.
            session: A driver session for the work.
        """

        # Set rank order of sense within entry
        rank = idx + 1

        # Gather kanji or readings this sense is restricted to
        stagks = [elem.text for elem in sense.findall('stagk')]
        stagrs = [elem.text for elem in sense.findall('stagr')]

        # If there are no restrictions, stagk/rs should refer to all k/rebs
        if not stagks:
            stagks = [elem.text for elem in entry.xpath('.//keb')]
            stagrs = [elem.text for elem in entry.xpath('.//reb')]

        # Gather parts of speech, fields of application, misc information
        # TODO: convert from codes to readable values OR
        #       create nodes that represent each of these to relate to
        pos = [elem.text for elem in sense.findall('pos')]
        fields = [elem.text for elem in sense.findall('field')]
        miscs = [elem.text for elem in sense.findall('misc')]

        # Gather other sense information
        s_infs = [elem.text for elem in sense.findall('s_inf')]

        # Gather various gloss lists by g_type
        defns = [elem.text for elem in sense.xpath('gloss[not(@g_type)]')]
        expls = [elem.text for elem in sense.xpath('gloss[@g_type="expl"]')]
        figs = [elem.text for elem in sense.xpath('gloss[@g_type="fig"]')]
        lits = [elem.text for elem in sense.xpath('gloss[@g_type="lit"]')]
        tms = [elem.text for elem in sense.xpath('gloss[@g_type="tm"]')]

        # Get the ent_seq of the containing entry
        ent_seq = int(entry.find('ent_seq').text)

        # Create the sense node and get its node ID
        with contextlib.ExitStack() as stack:
            session_ = session or stack.enter_context(self.driver.session())
            sense_id = session_.write_transaction(
                self._merge_and_return_sense,
                ent_seq,
                rank,
                pos,
                fields,
                miscs,
                s_infs,
                defns,
                expls,
                figs,
                lits,
                tms,
            )
            # TODO: relate stagks for sense
            logger.debug(
                'Added sense %s to entry %s',
                sense_id,
                ent_seq,
            )
            kanji_relationhips = session_.write_transaction(
                self._merge_kanji_sense_relationships,
                ent_seq,
                sense_id,
                stagks,
            )
            logger.debug(
                'Added sense relationships to kanji: %s',
                list(zip(stagks, kanji_relationhips)),
            )
            reading_relationships = session_.write_transaction(
                self._merge_reading_sense_relationships,
                ent_seq,
                sense_id,
                stagrs,
            )
            logger.debug(
                'Added sense relationships to readings: %s',
                list(zip(stagrs, reading_relationships)),
            )

        return sense_id

    @staticmethod
    def _merge_and_return_sense(
        tx: Transaction,
        ent_seq: int,
        rank: int,
        pos: List[str],
        fields: List[str],
        miscs: List[str],
        s_infs: List[str],
        defns: List[str],
        expls: List[str],
        figs: List[str],
        lits: List[str],
        tms: List[str],
    ) -> int:
        """Merges and returns sense related to entry `ent_seq`."""

        # Add a node for the entry
        cypher = textwrap.dedent("""\
            MATCH (e:Entry {ent_seq: $ent_seq})
            MERGE (e)-[:CONTAINS]->(s:Sense {ent_seq: $ent_seq, rank: $rank})
            ON CREATE
              SET s.pos = $pos
              SET s.field = $fields
              SET s.misc = $miscs
              SET s.s_inf = $s_infs
              SET s.defn = $defns
              SET s.expl = $expls
              SET s.fig = $figs
              SET s.lit = $lits
              SET s.tm = $tms
            RETURN id(s) AS node_id
        """)
        result = tx.run(
            cypher,
            ent_seq=ent_seq,
            rank=rank,
            pos=pos,
            fields=fields,
            miscs=miscs,
            s_infs=s_infs,
            defns=defns,
            expls=expls,
            figs=figs,
            lits=lits,
            tms=tms,
        )
        record = result.single()

        return record['node_id']

    @staticmethod
    def _merge_kanji_sense_relationships(
        tx: Transaction,
        ent_seq: int,
        sense_id: int,
        stagks: List[str],
    ) -> List[int]:
        """Merges kanji->sense relationships under `ent_seq`."""

        # Add a node for the entry
        cypher = textwrap.dedent("""\
            MATCH (e:Entry {ent_seq: $ent_seq})
            UNWIND $stagks as keb
            MATCH (s:Sense)<-[:CONTAINS]-(e)-[:CONTAINS]->
                  (k:Kanji {keb: keb})
            WHERE id(s) = $sense_id
            MERGE (k)-[r:HAS_SENSE]->(s)
            RETURN id(r) AS node_id
        """)
        result = tx.run(
            cypher,
            ent_seq=ent_seq,
            sense_id=sense_id,
            stagks=stagks,
        )
        return result.value()

    @staticmethod
    def _merge_reading_sense_relationships(
        tx: Transaction,
        ent_seq: int,
        sense_id: int,
        stagrs: List[str],
    ) -> List[int]:
        """Merges reading->sense relationships under `ent_seq`."""

        # Add a node for the entry
        cypher = textwrap.dedent("""\
            MATCH (e:Entry {ent_seq: $ent_seq})
            UNWIND $stagrs as reb
            MATCH (s:Sense)<-[:CONTAINS]-(e)-[:CONTAINS]->
                  (k:Reading {reb: reb})
            WHERE id(s) = $sense_id
            MERGE (k)-[r:HAS_SENSE]->(s)
            RETURN id(r) AS node_id
        """)
        result = tx.run(
            cypher,
            ent_seq=ent_seq,
            sense_id=sense_id,
            stagrs=stagrs,
        )
        return result.value()

    def add_lsource_for_sense(
        self,
        lsource: etree.Element,
        sense_id: int,
        session: Optional[Session] = None,
    ) -> int:
        """Adds an `lsource` related to sense node `sense_id` in the database.

        Args:
            lsource: The lsource element.
            sense_id: The associated sense node ID.
            session: A driver session for the work.

        Returns:
            Tuple of:
             - Merged Language node.
             - Merged Sense->Language relationship.
        """

        # Standard XML namespace
        ns = 'http://www.w3.org/XML/1998/namespace'

        # Parse lang, and check for ls_type and ls_wasei
        lang = lsource.attrib.get(f'{{{ns}}}lang', 'eng')
        partial = lsource.attrib.get('ls_type', 'full') == 'partial'
        wasei = lsource.attrib.get('ls_wasei', 'n') == 'y'

        # Get the source language word or phrase
        phrase = lsource.text

        with contextlib.ExitStack() as stack:
            session_ = session or stack.enter_context(self.driver.session())
            lsource_id, relationship_id = session_.write_transaction(
                self._merge_and_return_lsource,
                sense_id,
                lang,
                phrase,
                partial,
                wasei,
            )
            logger.debug(
                'Added lsource %s for sense with ID %s',
                lang,
                sense_id,
            )
            return lsource_id, relationship_id

    @staticmethod
    def _merge_and_return_lsource(
        tx: Transaction,
        sense_id: int,
        lang: str,
        phrase: str,
        partial: bool,
        wasei: bool,
    ) -> Tuple[int, int]:
        """Merges and returns lsource for sense `sense_id` in the database."""

        # Add a node for the entry
        cypher = textwrap.dedent("""\
            MATCH (s:Sense)
            WHERE id(s) = $sense_id
            MERGE (l:Language {lang: $lang})
            MERGE (s)-[r:SOURCED_FROM]->(l)
            ON CREATE
              SET r.phrase = $phrase
              SET r.partial = $partial
              SET r.wasei = $wasei
            RETURN id(l) AS node_id, id(r) as relationship_id
        """)
        result = tx.run(
            cypher,
            sense_id=sense_id,
            lang=lang,
            phrase=phrase,
            partial=partial,
            wasei=wasei,
        )
        record = result.single()
        return record['node_id'], record['relationship_id']

    def add_example_for_sense(
        self,
        example: etree.Element,
        sense_id: int,
        session: Optional[Session] = None,
    ) -> int:
        """Adds an `example` for `sense` to the database.

        Args:
            example: The example element.
            sense_id: The associated sense node ID.
            session: A driver session for the work.
        """

        # <example>
        # <ex_srce exsrc_type="tat">100041</ex_srce>
        # <ex_text>学位</ex_text>
        # <ex_sent xml:lang="jpn">彼は法学修士の学位を得た。</ex_sent>
        # <ex_sent xml:lang="eng">He got a master's degree in law.</ex_sent>
        # </example>

        # Parse out Tatoeba sequence number (validating it is Tatoeba Project)
        ex_srce = example.find('ex_srce')
        exsrc_type = ex_srce.attrib.get('exsrc_type', 'tat')
        assert exsrc_type == 'tat', f'Unexpected source type {exsrc_type!r}'
        tat = ex_srce.text

        # Standard XML namespace
        ns = 'http://www.w3.org/XML/1998/namespace'

        # Parse out the example sentences
        # NOTE: The following test was used to confirm examples come in pairs
        # > grep '<ex_sent' JMdict_e_examp.xml -n | \
        # >>  cut -d: -f1 | awk 'NR > 1 { print $0 - prev } { prev = $0 }' | \
        # >>  awk 'NR%2==1 { print $0 }' | \
        # >>  uniq
        # 1

        def lang(elem):
            return elem.attrib.get(f'{{{ns}}}lang', 'eng')

        ex_sents = {lang(el): el.text for el in example.findall('ex_sent')}

        # Parse out the example text for this sense_id
        ex_text = example.find('ex_text').text

        with contextlib.ExitStack() as stack:
            session_ = session or stack.enter_context(self.driver.session())
            lsource_id, relationship_id = session_.write_transaction(
                self._merge_and_return_example,
                sense_id,
                tat,
                ex_sents,
                ex_text,
            )
            logger.debug(
                'Added example %s for sense with ID %s',
                example,
                sense_id,
            )
            return lsource_id, relationship_id

    @staticmethod
    def _merge_and_return_example(
        tx: Transaction,
        sense_id: int,
        tat: int,
        ex_sents: Dict[str, str],
        ex_text: str,
    ) -> Tuple[int, int]:
        """Merges and returns lsource for sense `sense_id` in the database."""

        # Convert Dict[str, str] to eng and jpn ex_sent str
        eng = ex_sents['eng']
        jpn = ex_sents['jpn']

        # Add a node for the entry
        cypher = textwrap.dedent("""\
            MATCH (s:Sense)
            WHERE id(s) = $sense_id
            MERGE (e:Example {tat: $tat})
            MERGE (s)-[r:USED_IN]->(e)
            ON CREATE
              SET r.ex_text = $ex_text
              SET e.eng = $eng
              SET e.jpn = $jpn
            RETURN id(e) AS node_id, id(r) as relationship_id
        """)
        result = tx.run(
            cypher,
            sense_id=sense_id,
            tat=tat,
            ex_text=ex_text,
            eng=eng,
            jpn=jpn,
        )
        record = result.single()
        return record['node_id'], record['relationship_id']

    def add_ref(
        self,
        ref: etree.Element,
        session: Optional[Session] = None,
    ) -> int:
        """Adds an `ref` (either ``xref`` or ``ant`` to the database.

        Args:
            ref: The ref element.
            session: A driver session for the work.
        """

        # Get the parent sense and grandparent entry elements
        sense = ref.getparent()
        entry = sense.getparent()

        # Get the ent_seq and sense rank to find unique sense
        ent_seq = int(entry.find('ent_seq').text)
        rank = entry.findall('sense').index(sense) + 1

        # Parse the ref text and figure out whether it is an antonym ref
        data = parse_xref(ref.text)
        antonym = ref.tag == 'ant'

        with contextlib.ExitStack() as stack:
            session_ = session or stack.enter_context(self.driver.session())
            xref_ids = session_.write_transaction(
                self._merge_ref_relationships,
                ent_seq,
                rank,
                antonym,
                **data,
            )
            logger.debug(
                'Added x-reference relationships for %r under entry %s: %s',
                ref.text,
                ent_seq,
                xref_ids,
            )
            return xref_ids

    @staticmethod
    def _merge_ref_relationships(
        tx: Transaction,
        ent_seq: int,
        rank: int,
        antonym: bool,
        keb: Optional[str] = None,
        reb: Optional[str] = None,
        sense: Optional[int] = None,
    ) -> List[int]:
        """Merges and returns xrefs under entry with `ent_seq` in db."""

        # 1. find the kanji
        # 2. find the parent entries
        # 3. order those entries by ent_seq number
        # 4. relate the sense to only the kanji that is beneath the 1st entry

        cypher = textwrap.dedent("""\
            MATCH (r:Reading)-[:CONTAINS]-(e:Entry)-[:CONTAINS]->(k:Kanji)
            WHERE
              r.reb = coalesce($reb, r.reb) AND
              ($keb IS NULL OR (k IS NOT NULL AND k.keb = $keb))
            WITH e
            ORDER BY e.ent_seq
            LIMIT 1
            MATCH (src:Sense {ent_seq: $ent_seq, rank: $rank})
            WITH e, src
            MATCH (e)-[:CONTAINS]->(dest:Sense)
            WHERE $sense IS NULL OR dest.rank = $sense
            MERGE (src)-[xref:RELATED_TO {antonym: $antonym}]->(dest)
            RETURN id(xref) as relationship_id
        """)
        result = tx.run(
            cypher,
            ent_seq=ent_seq,
            rank=rank,
            antonym=antonym,
            keb=keb,
            reb=reb,
            sense=sense,
        )
        return result.value('relationship_id')


def get_parser(argv: List[str]) -> argparse.ArgumentParser:
    """Gets an argument parser for the main program.

    Args:
        argv: Argument list.

    Returns:
        The argument parser.
    """

    parser = argparse.ArgumentParser(description='JMdict parser to Neo4j')
    parser.add_argument('xml_file', help='JMdict XML file to parse')
    parser.add_argument(
        '-n',
        '--neo4j-uri',
        default='neo4j://localhost:7687',
        help='Neo4j URI string',
    )
    parser.add_argument('-u', '--user', default='neo4j', help='Neo4j user')
    parser.add_argument('-p', '--pw', default='japanese', help='Neo4j pw')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-d', '--debug', action='store_true',
                       help='Display debug log messages')
    group.add_argument('-s', '--silent', action='store_true',
                       help='Display only warning log messages')

    parser.add_argument('--neo4j-debug', action='store_true',
                        help='Display Neo4j driver debug messages')

    parser.add_argument('--skip-entries', action='store_true',
                        help='Skip over operations on entry elements')
    parser.add_argument('--skip-kanji', action='store_true',
                        help='Skip over operations on kanji elements')
    parser.add_argument('--skip-readings', action='store_true',
                        help='Skip over operations on reading elements')
    parser.add_argument('--skip-senses', action='store_true',
                        help='Skip over operations on sense elements')
    parser.add_argument('--skip-refs', action='store_true',
                        help='Skip over operations on xref/ant elements')

    return parser


def grouper(
    iterable: Iterable[etree.Element],
    n: int,
) -> Iterable[Sequence[etree.Element]]:
    """Groups `iterable` into sequences of length `n`.

    Args:
        iterable: The object to divide into groups.
        n: The group size.

    Returns:
        Iterable of sequences of length `n`.
    """
    args = [iter(iterable)] * n
    return itertools.zip_longest(*args)


def configure_logger(log: logging.Logger, level: str):
    """Configures `log` to use logging level `level`.

    Args:
        log: The logger to configure.
        level: The logging level to use.
    """

    # Set the log level
    log.setLevel(level)

    # Create the handler and set its level
    handler = logging.StreamHandler()
    handler.setLevel(level)

    # Create the formatter and add it to the handler
    formatter = logging.Formatter(fmt='%(levelname)s: %(message)s')
    handler.setFormatter(formatter)

    # Add the configured handler to the logger
    log.addHandler(handler)


def main(argv=sys.argv[1:]):
    """Does it all."""

    # Parse arguments
    args = get_parser(argv).parse_args()

    # Configure logging
    level = 'DEBUG' if args.debug else 'WARNING' if args.silent else 'INFO'
    configure_logger(logger, level)

    # Configure Neo4j logging
    neo4j_logger = logging.getLogger('neo4j')
    neo4j_level = 'DEBUG' if args.neo4j_debug else 'WARNING'
    configure_logger(neo4j_logger, neo4j_level)

    # Read the specified XML file and parse the XML tree
    with open(args.xml_file) as xmlf:
        tree = etree.parse(xmlf)

    # Get the tree's root element for traversal
    root = tree.getroot()

    # Create a Neo4j GraphApp instance
    neo_app = NeoApp(args.neo4j_uri, args.user, args.pw)

    # Set constraints for DB schema
    neo_app.create_entry_constraint()
    neo_app.create_lsource_constraint()
    neo_app.create_example_constraint()

    # Create indices for DB schema
    neo_app.create_kanji_index()
    neo_app.create_reading_index()
    neo_app.create_sense_index()

    # Traverse from root on <entry> elements and add nodes
    now = datetime.datetime.now()
    for num, batch in enumerate(grouper(root.iter('entry'), 1024)):
        logger.info(
            'Processing entry batch: %s, elapsed time: %s',
            num + 1,
            datetime.datetime.now() - now,
        )
        with neo_app.driver.session() as session:
            for entry in batch:
                if entry is None:
                    break
                if not args.skip_entries:
                    neo_app.add_entry(entry, session)

                if not args.skip_kanji:
                    for k_ele in entry.findall('k_ele'):
                        neo_app.add_kanji_for_entry(k_ele, entry, session)

                if not args.skip_readings:
                    for r_ele in entry.findall('r_ele'):
                        neo_app.add_reading_for_entry(r_ele, entry, session)

                if not args.skip_senses:
                    for idx, sense in enumerate(entry.findall('sense')):
                        sense_id = neo_app.add_sense_for_entry(
                            idx,
                            sense,
                            entry,
                            session,
                        )

                        for lsource in sense.findall('lsource'):
                            neo_app.add_lsource_for_sense(
                                lsource,
                                sense_id,
                                session,
                            )

                        for example in sense.findall('example'):
                            neo_app.add_example_for_sense(
                                example,
                                sense_id,
                                session,
                            )

    if not args.skip_refs:
        xref_or_ant = './/*[self::xref or self::ant]'
        for num, batch in enumerate(grouper(root.xpath(xref_or_ant), 1024)):
            logger.info(
                'Processing ref batch: %s, elapsed time: %s',
                num + 1,
                datetime.datetime.now() - now,
            )
            with neo_app.driver.session() as session:
                for ref in batch:
                    if ref is None:
                        break
                    neo_app.add_ref(ref, session)

    logger.info('Total elapsed time: %s', datetime.datetime.now() - now)

    # Close the neo_app
    neo_app.close()


if __name__ == '__main__':
    main()
