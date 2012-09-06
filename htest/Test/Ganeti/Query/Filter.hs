{-# LANGUAGE TemplateHaskell #-}
{-# OPTIONS_GHC -fno-warn-orphans #-}

{-| Unittests for ganeti-htools.

-}

{-

Copyright (C) 2009, 2010, 2011, 2012 Google Inc.

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
02110-1301, USA.

-}

module Test.Ganeti.Query.Filter (testQuery_Filter) where

import Test.QuickCheck hiding (Result)
import Test.QuickCheck.Monadic

import qualified Data.Map as Map
import Data.List
import Text.JSON (showJSON)

import Test.Ganeti.TestHelper
import Test.Ganeti.TestCommon
import Test.Ganeti.Objects (genEmptyCluster)

import Ganeti.BasicTypes
import Ganeti.JSON
import Ganeti.Objects
import Ganeti.Query.Language
import Ganeti.Query.Query

-- * Helpers

-- | Run a query and check that we got a specific response.
checkQueryResults :: ConfigData -> Query -> String
                  -> [[ResultEntry]] -> Property
checkQueryResults cfg qr descr expected = monadicIO $ do
  result <- run (query cfg qr) >>= resultProp
  stop $ printTestCase ("Inconsistent results in " ++ descr)
         (qresData result ==? expected)

-- | Makes a node name query, given a filter.
makeNodeQuery :: Filter FilterField -> Query
makeNodeQuery = Query QRNode ["name"]

-- | Checks if a given operation failed.
expectBadQuery :: ConfigData -> Query -> String -> Property
expectBadQuery cfg qr descr = monadicIO $ do
  result <- run (query cfg qr)
  case result of
    Bad _ -> return ()
    Ok a  -> stop . failTest $ "Expected failure in " ++ descr ++
                               " but got " ++ show a

-- * Test cases

-- | Tests single node filtering: eq should return it, and (lt and gt)
-- should fail.
prop_node_single_filter :: Property
prop_node_single_filter =
  forAll (choose (1, maxNodes)) $ \numnodes ->
  forAll (genEmptyCluster numnodes) $ \cfg ->
  let allnodes = Map.keys . fromContainer $ configNodes cfg in
  forAll (elements allnodes) $ \nname ->
  let fvalue = QuotedString nname
      buildflt n = n "name" fvalue
      expsingle = [[ResultEntry RSNormal (Just (showJSON nname))]]
      othernodes = nname `delete` allnodes
      expnot = map ((:[]) . ResultEntry RSNormal . Just . showJSON) othernodes
      test_query = checkQueryResults cfg . makeNodeQuery
  in conjoin
       [ test_query (buildflt EQFilter) "single-name 'EQ' filter" expsingle
       , test_query (NotFilter (buildflt EQFilter))
         "single-name 'NOT EQ' filter" expnot
       , test_query (AndFilter [buildflt LTFilter, buildflt GTFilter])
         "single-name 'AND [LT,GT]' filter" []
       , test_query (AndFilter [buildflt LEFilter, buildflt GEFilter])
         "single-name 'And [LE,GE]' filter" expsingle
       ]

-- | Tests node filtering based on name equality: many 'OrFilter'
-- should return all results combined, many 'AndFilter' together
-- should return nothing. Note that we need at least 2 nodes so that
-- the 'AndFilter' case breaks.
prop_node_many_filter :: Property
prop_node_many_filter =
  forAll (choose (2, maxNodes)) $ \numnodes ->
  forAll (genEmptyCluster numnodes) $ \cfg ->
  let nnames = Map.keys . fromContainer $ configNodes cfg
      eqfilter = map (EQFilter "name" . QuotedString) nnames
      alln = map ((:[]) . ResultEntry RSNormal . Just . showJSON) nnames
      test_query = checkQueryResults cfg . makeNodeQuery
      num_zero = NumericValue 0
  in conjoin
     [ test_query (OrFilter eqfilter) "all nodes 'Or' name filter" alln
     , test_query (AndFilter eqfilter) "all nodes 'And' name filter" []
     -- this next test works only because genEmptyCluster generates a
     -- cluster with no instances
     , test_query (EQFilter "pinst_cnt" num_zero) "pinst_cnt 'Eq' 0" alln
     , test_query (GTFilter "sinst_cnt" num_zero) "sinst_cnt 'GT' 0" []
     ]

-- | Tests node regex filtering. This is a very basic test :(
prop_node_regex_filter :: Property
prop_node_regex_filter =
  forAll (choose (0, maxNodes)) $ \numnodes ->
  forAll (genEmptyCluster numnodes) $ \cfg ->
  let nnames = Map.keys . fromContainer $ configNodes cfg
      expected = map ((:[]) . ResultEntry RSNormal . Just . showJSON) nnames
      regex = mkRegex ".*"::Result FilterRegex
  in case regex of
       Bad msg -> failTest $ "Can't build regex?! Error: " ++ msg
       Ok rx ->
         checkQueryResults cfg (makeNodeQuery (RegexpFilter "name" rx))
           "Inconsistent result rows for all nodes regexp filter"
           expected

-- | Tests node regex filtering. This is a very basic test :(
prop_node_bad_filter :: String -> Int -> Property
prop_node_bad_filter rndname rndint =
  forAll (choose (1, maxNodes)) $ \numnodes ->
  forAll (genEmptyCluster numnodes) $ \cfg ->
  let regex = mkRegex ".*"::Result FilterRegex
      test_query = expectBadQuery cfg . makeNodeQuery
      string_value = QuotedString rndname
      numeric_value = NumericValue $ fromIntegral rndint
  in case regex of
       Bad msg -> failTest $ "Can't build regex?! Error: " ++ msg
       Ok rx ->
         conjoin
           [ test_query (RegexpFilter "offline" rx)
             "regex filter against boolean field"
           , test_query (EQFilter "name" numeric_value)
             "numeric value eq against string field"
           , test_query (TrueFilter "name")
             "true filter against string field"
           , test_query (EQFilter "offline" string_value)
             "quoted string eq against boolean field"
           , test_query (ContainsFilter "name" string_value)
             "quoted string in non-list field"
           , test_query (ContainsFilter "name" numeric_value)
             "numeric value in non-list field"
           ]

testSuite "Query/Filter"
  [ 'prop_node_single_filter
  , 'prop_node_many_filter
  , 'prop_node_regex_filter
  , 'prop_node_bad_filter
  ]