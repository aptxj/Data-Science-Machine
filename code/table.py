import pdb
from sqlalchemy.orm import sessionmaker
from compile_query import compile_query
from sqlalchemy import text

import sqlalchemy.dialects.mysql.base as column_datatypes
from sqlalchemy.schema import Table

from sqlalchemy.schema import MetaData

import datetime
import hashlib
import copy


DEFAULT_METADATA = {
    'path' : [], 
    'numeric' : False,
    'categorical' : False,
    
    # 'feature_type' : 'original', #original, row, agg, flat
    # 'funcs_applied' : [],

    # 'agg_feature_type' : None,
    # 'row_feature_type' : None,
    
    # 'allowed_agg_funcs' : None,
    # 'excluded_agg_funcs' : set([]),
    # 'allowed_row_funcs' : None,
    # 'excluded_row_funcs' : set([]),
}


class DSMTable:
    def __init__(self, table, db):
        self.db = db
        self.table = table
        self.engine = self.table.bind
        self.session = sessionmaker(bind=self.engine)()

        self.primary_key_names = [key.name for key in table.primary_key]

        self.columns = {}
        self.cols_to_add = []
        self.cols_to_drop = []
        
        self.has_row_features = False
        self.has_agg_features = False
        self.has_flat_features = False

        self.init_columns()


    def init_columns(self):
        """
        make metadata for columns already in database and return the metadata dictionary
        """
        datatypes = [column_datatypes.INTEGER, column_datatypes.FLOAT, column_datatypes.DECIMAL, column_datatypes.DOUBLE, column_datatypes.SMALLINT, column_datatypes.MEDIUMINT]
        # categorical = self.get_categorical()
        # if len(categorical) > 0:
        #     pdb.set_trace()

        for col in self.table.c:
            col = DSMColumn(col, table=self)

            col.update_metadata({
                'numeric' : type(col.type) in datatypes and not (col.primary_key or col.has_foreign_key),
            })

            self.columns[col.name] = col


    #############################
    # Database operations       #
    #############################
    def create_column(self, column_name, column_type, metadata={},flush=False, drop_if_exists=True):
        """
        add column with name column_name of type column_type to this table. if column exists, drop first

        todo: suport where to add it
        """
        self.cols_to_add.append((column_name, column_type, metadata))
        if flush:
            self.flush_columns(drop_if_exists=drop_if_exists)
    
    def drop_column(self, column_name, flush=False):
        """
        drop column with name column_name from this table
        """
        self.cols_to_drop.append(column_name)
        if flush:
            self.flush_columns(drop_if_exists=drop_if_exists)

    def flush_columns(self, drop_if_exists=True):
        # print self.cols_to_drop, self.cols_to_add
        #first, check which of cols_to_add need to be dropped first
        for (name, col_type, metadata) in self.cols_to_add:
            if drop_if_exists and self.has_column(name):
                self.drop_column(name)

        #second, flush columns that need to be dropped
        values = []
        for name in self.cols_to_drop:
            del self.columns[name]
            values.append("DROP `%s`" % (name))
        if len(values) > 0:
            values = ", ".join(values)

            self.engine.execute(
                """
                ALTER TABLE `{table}`
                {cols_to_drop}
                """.format(table=self.table.name, cols_to_drop=values)
            ) #very bad, fix how parameters are substituted in

            self.cols_to_drop = []
        
        #third, flush columns that need to be added
        values = []
        new_col_metadata = {}
        for (name, col_type, metadata) in self.cols_to_add:
            new_col_metadata[name] = metadata
            values.append("ADD COLUMN `%s` %s" % (name, col_type))
        if len(values) > 0:
            values = ", ".join(values)
            qry = """
                ALTER TABLE `{table}`
                {cols_to_add}
                """.format(table=self.table.name, cols_to_add=values)
            self.engine.execute(qry) #very bad, fix how parameters are substituted in
            self.cols_to_add = []

        #reflect table again to have update columns
        # TODO check to make sure old column reference still work
        self.table = Table(self.table.name, MetaData(bind=self.engine), autoload=True, autoload_with=self.engine)
        
        for c in self.table.c:
            if c.name not in self.columns:
                    # pdb.set_trace()
                    col = DSMColumn(c, self, metadata=new_col_metadata[c.name])
                    self.columns[col.name] = col


    ###############################
    # Table info helper functions #
    ###############################
    def get_column_info(self, prefix='', ignore_relationships=False, match_func=None, first=False, set_trace=False):
        """
        return info about columns in this table. 
        info should be things that are read directly from database or something that is dynamic at query time. everything else should be part of metadata

        """
        cols = []
        # pdb.set_trace()
        for col in self.columns.values():
            if ignore_relationships and col.primary_key:
                continue

            if ignore_relationships and col.has_foreign_key:
                continue

            if set_trace:    
                pdb.set_trace()

            if match_func != None and not match_func(col):
                continue

            if first:
                return col

            cols.append(col)
        
        return cols

    def get_columns_of_type(self, datatypes=[], **kwargs):
        """
        returns a list of columns that are type data_type
        """
        if type(datatypes) != list:
            datatypes = [datatypes]
        return [c for c in self.get_column_info(**kwargs) if type(c.type) in datatypes]

    def get_numeric_columns(self, **kwargs):
        """
        gets columns that are numeric as specified by metada
        """
        return [c for c in self.get_column_info(**kwargs) if c.metadata['numeric']]
    
    def has_column(self, name):
        return name in self.columns

    def get_categorical(self, max_proportion_unique=.3, min_proportion_unique=0, max_num_unique=10):
        cols = self.get_column_info()
        counts = self.get_num_distinct(cols)
        
        qry = """
        SELECT COUNT(*) from `{table}`
        """.format(table=self.table.name)
        total = float(self.engine.execute(qry).fetchall()[0][0])

        if total == 0:
            return set([])

        cat_cols = []
        for col, count in counts:
            if ( max_num_unique > count > 1 and
                 max_proportion_unique <= count/total < min_proportion_unique and
                 len(col.metadata['path']) <= 1 ):

                cat_cols.append(col)

        return cat_cols

    def get_num_distinct(self, cols):
        """
        returns number of distinct values for each column in cols. returns in same order as cols_to_drop
        """
        column_names = [c.name for c in cols]
        SELECT = ','.join(["count(distinct(`%s`))"%c for c in column_names])

        qry = """
        SELECT {SELECT} from `{table}`
        """.format(SELECT=SELECT, table=self.table.name)

        counts = self.engine.execute(qry).fetchall()[0]
        
        return zip(cols,counts)

    def get_rows(self, cols):
        """
        return rows with values for the columns specificed by col
        """
        column_names = [c.name for c in cols]

        SELECT = ','.join([("`%s`"%c) for c in column_names])

        pk = self.get_column_info(match_func= lambda x: x.primary_key, first=True)

        qry = """
        SELECT {SELECT} from `{table}` ORDER BY `{primary_key}`
        """.format(SELECT=SELECT, table=self.table.name, primary_key=pk.name)
        rows = self.engine.execute(qry)

        return self.engine.execute(qry)


    def get_rows_as_dict(self, cols):
        """
        return rows with values for the columns specificed by col
        """
        rows = self.get_rows(cols)
        rows = [dict(r) for r in rows.fetchall()]
        return rows


class DSMColumn():
    def __init__(self, column, table, metadata=None):
        self.table = table
        self.column = column
        # self.name = hashlib.sha224(column.name).hexdigest()
        self.name = column.name
        self.unique_name =  self.table.table.name + '.' + self.name
        self.type = column.type
        self.primary_key = self.column.primary_key
        self.has_foreign_key = len(column.foreign_keys)>0
        
        self.metadata = metadata
        if not metadata:
            self.metadata = dict(DEFAULT_METADATA)

    def update_metadata(self, update):
        self.metadata.update(update)

    def copy_metadata(self):
        """
        returns copy of the metadata object for this column_metadata
        """
        return dict(self.metadata)


    def get_distinct_vals(self):
        #try to get cached distinct_vals
        if 'distinct_vals' in self.metadata:
            return self.metadata['distinct_vals']

        qry = """
        SELECT distinct(`{col_name}`) from `{table}`
        """.format(col_name=self.name, table=self.table.table.name)

        distinct = self.table.engine.execute(qry).fetchall()

        vals = []
        for d in distinct:
            d = d[0]

            if d in ["", None]:
                continue

            if type(d) == datetime.datetime:
                continue

            if type(d) == long:
                d = int(d)

            if d == "\x00":
                d = False
            elif d == "\x01":
                d = True

            vals.append(d)

        #save distinct vals to cache
        self.metadata['distinct_vals'] = vals
        
        return vals      

    def get_max_col_val(self):
        qry = "SELECT MAX(`{col_name}`) from `{table}`".format(col_name=self.name, table=self.table.table.name)
        result = self.table.engine.execute(qry).fetchall()
        return result[0][0]

    def prefix_name(self, prefix):
        return prefix + self.name